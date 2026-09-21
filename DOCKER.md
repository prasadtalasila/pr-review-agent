# 🐳 Running this project in a container

One image, `docker/Dockerfile`, built for **development**: the full local gate
(pytest, ruff, pylint, pyright, `poetry build`) and a runnable agent, on a
machine that need not have Python 3.13, git 2.32+, Poetry, Node or the
`claude` CLI installed on it.

It carries the toolchain, not the code. The checkout is bind-mounted at
`/workspace`, so the code the container runs is the code you are editing and a
rebuild is needed only when the *toolchain* changes — `poetry.lock`, the
Dockerfile, the pinned `claude` version.

| | |
| :-- | :-- |
| Base | `node:26.6.0-trixie` — Debian trixie, Python 3.13, git 2.47 |
| Size | ~1.4GB |
| Carries | `poetry.lock` installed into `/opt/venv`, Poetry, the `claude` CLI, git |
| Mounts | your checkout at `/workspace`, `~/.claude` for the login |
| Built by CI | **No.** Nothing in `.github/workflows` builds it, so a break here is caught by hand or not at all |

**This is not a deployment image.** It mounts a checkout, installs the
package editable from it, and gives the account inside your own uid — all
right for a developer and wrong for a host that runs the reviewer
unattended, which wants the published wheel, no source tree and no Poetry.
Nothing here prevents that image existing later; it is simply not this one.
The two jobs are combined for now because a developer needs the engine path
runnable anyway, and a second image nobody has run is worse than none.

## 🔧 Build and run

`docker compose` reads `.env` from its own directory, so run it from
`docker/`:

```bash
cd docker
cp .env.example .env
$EDITOR .env          # PRA_USER / PRA_UID / PRA_GID are required
mkdir -p claude       # the mount point for the container's ~/.claude

docker compose up -d
docker compose exec dev bash
```

`.env.example` documents every variable the compose file reads. Three of them
have no default and the stack refuses to start without them — the account
inside the image and its numeric ids. That is deliberate: the account is a
property of your machine, and a default that is wrong for almost everyone
still *works*, it just writes files owned by the wrong uid into the checkout
you mounted.

Without compose:

```bash
docker build -t pr-review-agent-dev -f docker/Dockerfile \
  --build-arg PRA_USER="$(id -un)" \
  --build-arg PRA_UID="$(id -u)" --build-arg PRA_GID="$(id -g)" .
docker run -it --rm --init -v "$(pwd)":/workspace pr-review-agent-dev
```

`--init` is not optional. See ["Why PID 1 has to be a real
init"](#-why-pid-1-has-to-be-a-real-init).

## ✅ Verify the container

Everything below was run inside the image on 2026-09-21, and re-running it is
what this file asks of anyone who changes `docker/Dockerfile` or
`docker/entrypoint.sh`. The toolchain first:

```bash
docker compose exec dev bash -c 'python --version; git --version; claude --version'
# Python 3.13.5 / git version 2.47.3 / 2.1.274 (Claude Code)
```

Then the local gate from [DEVELOPER.md](DEVELOPER.md), which is the real
check — it is the same commands, run in the same way, and it passes:

```bash
docker compose exec dev bash -c 'cd /workspace && poetry run pytest --cov --cov-report=term-missing'
# 789 passed, 1 deselected, 31 warnings in 75.13s -- TOTAL coverage 97%
docker compose exec dev bash -c 'cd /workspace && poetry run ruff format --check . && poetry run ruff check .'
# 76 files already formatted / All checks passed!
docker compose exec dev bash -c 'cd /workspace && poetry run pylint src --rcfile=.pylintrc --fail-under=9.0'
# rated at 9.95/10
docker compose exec dev bash -c 'cd /workspace && poetry run pyright src tests'
# 0 errors, 0 warnings, 0 informations
docker compose exec dev bash -c 'cd /workspace && poetry build'
# Built pr_review_agent-0.16.1-py3-none-any.whl
```

And the command the wheel ships, in an empty directory, exactly as the
quickstart tells an operator to run it:

```bash
docker compose exec dev bash -c 'mkdir -p /tmp/smoke && cd /tmp/smoke &&
  pr-review-agent config generate && pr-review-agent config validate'
# wrote config.yaml / config.yaml is valid
```

`pr-review-agent host check` without a token exits `3` and says
`GITHUB_TOKEN is not set`, which is the documented behaviour rather than a
container fault.

## 🧰 How the toolchain is arranged inside

One virtual environment, `/opt/venv`, first on `PATH` and owned by the
unprivileged account — so `python`, `pip`, `poetry`, `pytest` and
`pr-review-agent` are all the same environment, and nothing in it needs root.

`POETRY_VIRTUALENVS_CREATE=false` is what reconciles that with `poetry.toml`'s
`in-project = true`. Without it, `poetry install` would build a *second* venv
at `/workspace/.venv` — inside the bind mount, colliding with the `.venv` your
host already has there, and resolved against a different interpreter. With it,
Poetry installs into the active environment and every `poetry run ...` command
in [DEVELOPER.md](DEVELOPER.md) works verbatim inside the container.

The image is built with `poetry install --no-root`, because at build time
there is no package to install: `src/` arrives with the mount. So
`entrypoint.sh` runs `pip install --no-deps --editable /workspace` on every
start — the one step that cannot happen before the mount exists, and what puts
the `pr-review-agent` command on the path.

Debian's `/etc/profile` *assigns* `PATH` rather than extending it, so a login
shell (`bash -l`, `su - <user>`, an editor's remote shell) would otherwise
start with the venv gone and report `poetry: command not found` from a
container that demonstrably has Poetry in it. `/etc/profile.d/pr-review-agent-venv.sh`
is what survives that.

## 🧟 Why PID 1 has to be a real init

`init: true` in the compose file (`--init` on a bare `docker run`) is
load-bearing, and the symptom of leaving it out looks like a flaky test rather
than a container problem.

The suite spawns `git` through asyncio, each child in its own session. When
one finishes it reparents to PID 1, and a PID 1 that never calls `wait()` —
`sleep infinity`, the obvious choice for a container you exec into — leaves
every one of them as a zombie. About 200 accumulate by the time the suite
reaches `tests/test_worker.py`, and the container is then SIGKILLed out from
under the run: `docker compose exec` returns 137, pytest's output stops
mid-file, and nothing says why. `docker logs` is empty, `docker inspect`
reports `OOMKilled=false`, and the cgroup's `memory.events` and `pids.events`
are both all zeroes — memory peaked at 146MB against no limit, and
`pids.current` at ~200 against a `pids.max` of 272806.

Measured, not reasoned about: five runs of `tests/test_worker.py` with
`sleep infinity` as PID 1 died three times and passed twice. Five runs with
`--init` passed five times, and the zombies are reaped as they appear. The
full suite then passes 789/789.

## 🌳 A git worktree needs its main repository mounted too

Mounting a worktree rather than a clone is the one setup that does not work
out of the box, and it fails a long way from the cause:

```text
git fetch route: ... ls-remote ... exited 128:
fatal: not a git repository: /home/you/git/pr-review-agent/.git/worktrees/feature-x
```

A worktree's `.git` is a *file* holding an absolute path into the main
repository's `.git/worktrees/`, and that path does not exist inside the
container. Every `git` command in the checkout fails, which
`tests/test_bootstrap.py::test_a_reachable_remote_passes` is the first to
notice.

The fix is to mount the main repository at the same absolute path it has on
the host. `docker-compose.override.yml` beside the compose file is picked up
automatically and is gitignored, so this stays local to the machine that needs
it:

```yaml
services:
  dev:
    volumes:
      - /home/you/git/pr-review-agent:/home/you/git/pr-review-agent:ro
```

Then point `PRA_WORKSPACE` in `.env` at the worktree.

## 🤖 Running the agent from the container

The image carries the `claude` CLI at the version
`engine.expected_version` names in `config.example.yaml`, so the engine
adapter finds the binary it was written against. What it does not carry is a
login: the `claude` credential lives in `~/.claude`, which the compose file
mounts from `PRA_CLAUDE_HOME` (default `docker/claude/`, gitignored). Log in
once, and a rebuild is not a re-authentication:

```bash
docker compose exec dev claude      # then /login, then Ctrl-D
```

After that, with a `config.yaml` in the checkout and a token in the
environment:

```bash
docker compose exec dev bash -c 'cd /workspace && pr-review-agent host check'
docker compose exec dev bash -c 'cd /workspace && pr-review-agent daemon start'
```

The usual warning applies with more force in a container than out of it: this
spends a real, shared allowance and posts under a real GitHub account. The
budget governor is in the container too, and `publish.dry_run` is the setting
that lets the whole pipeline run without a comment appearing.

## 🏃 One command instead of a shell

`docker compose run` is the other shape — one command, no long-lived
container, and the cheaper one for a quick check:

```bash
cd docker
docker compose run --rm dev pytest -q
docker compose run --rm dev ruff check /workspace
```

## 🔁 When to rebuild

`docker compose build` after a change to `poetry.lock`,
`docker/Dockerfile`, or `PRA_USER`/`PRA_UID`/`PRA_GID` — those three are build
args as well as runtime settings, so a restart alone will not move them.
Editing source code needs nothing: it is mounted.
