# Developer guide

How to set up, verify and build the project. The source code lives in
_src/pr_review_agent_ and the test suite in _tests_.

This document is about the *toolchain*. For what the code does and why it is
arranged that way, start at [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md);
[CLAUDE.md](CLAUDE.md) holds the behavioural rules that govern how a change is
made, and [AGENTS.md](AGENTS.md) the coding conventions.

## 🗂 Package layout

```text
src/pr_review_agent/
├── _compat.py         # the one Python 3.10 shim (enum.StrEnum)
├── _startup.py        # token and config, each loadable on its own
├── bootstrap.py       # pre-flight egress checks for a new host
├── budget.py          # rolling windows, the ladder, reserve-then-settle
├── config.py          # config.yaml → frozen dataclasses
├── daemon.py          # the poll-classify-enqueue loop
├── cli/
│   ├── __init__.py    # the root group, the nouns, the exit codes
│   ├── _common.py     # the shared --config option and startup handling
│   ├── cmd_config.py  # config generate | validate
│   ├── cmd_host.py    # host check
│   └── cmd_daemon.py  # daemon start
├── templates/         # the two config templates the wheel ships
├── queue.py           # claim protocol and per-pull-request leases
├── worker.py          # claim → review → settle → close the row
├── store.py           # SQLite: schema, watermarks, ETags, queue table
├── triggers/
│   ├── models.py      # payload-shaped dataclasses; PayloadError
│   ├── allowlist.py   # numeric-user-id membership
│   ├── mention.py     # @claude in *prose* only
│   └── classifier.py  # PullRequest | Comment → Decision
├── poller/
│   ├── endpoints.py   # the three repo-wide request paths, and /pulls/{n}
│   ├── client.py      # async conditional GET, rate-limit handling
│   ├── etag_store.py  # the ETagCache protocol + in-memory cache
│   ├── interval.py    # adaptive poll delay
│   ├── payloads.py    # raw GitHub dicts → trigger models
│   ├── pulls.py       # one pull request → PullRequestFacts
│   └── poller.py      # one sweep across all three endpoints
├── workspace/
│   ├── gitcmd.py      # the one hardened `git` invocation
│   └── repo.py        # bare mirror, per-run worktree, diff, teardown
└── engine/
    ├── models.py      # ReviewEngine protocol, Capabilities, request/result
    └── fake.py         # an engine that spends nothing, for tests
```

## 🐍 The Python 3.10 shim

Supporting Python 3.10 costs exactly one shim, in `_compat.py`: `enum.StrEnum`
arrived in 3.11. The replacement is *not* the obvious `class StrEnum(str, Enum)`
— on 3.10 that inherits `Enum.__str__`, so `str(member)` yields
`"Endpoint.OPEN_PULLS"` instead of `"open_pulls"`, and any interpolated log
line or persisted dict key would change meaning with the interpreter version.
`_compat.py` delegates `__str__` and `__format__` to `str` to restore the 3.11
behaviour, and `tests/test_compat.py` pins that parity.

Those assertions are the reason the CI matrix includes 3.10: it is the only job
where the shim is imported at all.

## 📦 Dependencies

The agent supports **Python 3.10 through 3.14** and uses:

- [PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation) — reads `config.yaml`.
  Loading goes through `yaml.safe_load` only.
- [httpx](https://www.python-httpx.org/) — the async HTTP client used by the
  poller for conditional (`If-None-Match`) GETs against the GitHub REST API.
  Its `MockTransport` is what lets the poller tests run with no network.
- [Click](https://click.palletsprojects.com/) — the `pr-review-agent <noun>
  <verb>` command tree. Chosen over `argparse`, which the two entry points
  used before 0.14, for consistency with the
  [DTaaS CLI](https://github.com/INTO-CPS-Association/DTaaS/tree/feature/distributed-demo/cli):
  same association, same language, same grammar, and a reviewer moving
  between the two should not meet two idioms for it.
- [Poetry](https://python-poetry.org/docs/) — manages dependencies and builds
  the package. The configuration is _pyproject.toml_; new dependencies are
  added there and locked into _poetry.lock_.
- [pytest](https://docs.pytest.org/) with
  [pytest-cov](https://pytest-cov.readthedocs.io/) and
  [pytest-asyncio](https://pytest-asyncio.readthedocs.io/) — the test suite.
  `asyncio_mode = "auto"` is set in _pyproject.toml_, so an `async def test_*`
  needs no marker.
- [Ruff](https://docs.astral.sh/ruff/) — formatting and fast linting.
- [Pylint](https://pylint.readthedocs.io/) — deeper static analysis, using the
  shared _.pylintrc_.
- [Pyright](https://github.com/microsoft/pyright) — static type checking,
  configured in _pyproject.toml_ under `[tool.pyright]`.

- [cryptography](https://cryptography.io/) — **dev only**. It generates the
  certificate for the loopback HTTPS server the git tests fetch from. The
  agent refuses every transport but https, so a `file://` fixture would not
  work; see [docs/WORKSPACE.md](docs/WORKSPACE.md).

`sqlite3` is in the standard library, so the store adds no dependency.

**`git` 2.32 or later must be on `PATH`.** The
[checkout](docs/WORKSPACE.md) shells out to it, and 2.32 is where
`GIT_CONFIG_GLOBAL` arrived — below that it is ignored without an error,
taking the checkout's hardening with it. The git-backed tests fail rather
than skip when it is missing, except on Windows, where they skip: the daemon
is deployed on Linux and Git for Windows differs in exec-path layout.

## ⚙️ Setup

**Poetry must be installed inside the project's own virtual environment. Do
not use a system-wide Poetry.**

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install poetry     # latest Poetry, into .venv
.venv/bin/poetry --version                 # expect 2.x
.venv/bin/poetry install                   # runtime + dev dependencies
```

On Windows the interpreter is `.venv\Scripts\python.exe` and Poetry is
`.venv\Scripts\poetry.exe`; everything else is identical.

Put `.venv/bin` first on `PATH` (or activate the venv) so a bare `poetry`
resolves to the project's copy, and check it before you trust it:

```bash
export PATH="$PWD/.venv/bin:$PATH"
command -v poetry        # must print <repo>/.venv/bin/poetry
```

### Why not a system-wide Poetry

The distribution-packaged Poetry is routinely years behind. Debian and Ubuntu
still ship 1.8, which cannot read this project at all: it rejects PEP 621
`[project]` metadata outright, and it cannot read a lock-version 2.1
`poetry.lock`. A system Poetry that is merely *older* rather than too old is
worse than one that fails loudly, because it silently resolves a different
dependency set than CI does, and the difference only surfaces as a CI failure
nobody can reproduce.

Pinning Poetry to the project venv also means the Poetry version is part of
the checkout, so every contributor and every CI job runs the same one. CI
bootstraps Poetry exactly as above and then asserts that `poetry` resolves
inside `.venv` before using it, so this rule cannot quietly rot.

`poetry install` installs the `dev` group by default. To install only the
runtime dependencies, use `poetry install --only main`. Avoid
`poetry install --sync`: because Poetry lives in the venv it manages, a sync
would uninstall Poetry itself.

The project is configured (via _poetry.toml_) to create its virtual
environment in `.venv/` inside the repository, so editors and CI find the same
interpreter. Prefix commands with `poetry run`, or open a subshell with
`poetry env activate`.

Run `poetry run pr-review-agent config generate` before running the daemon,
or copy `config.minimal.example.yaml` to `config.yaml` by hand — from a
clone the two are the same bytes. `config.example.yaml` is the
comprehensive one (`config generate --full`): every key the loader accepts,
with the reasoning behind each and the defaults shown.

Each template exists twice: at the repository root, which is what a clone
and the documentation's links use, and under
`src/pr_review_agent/templates/`, which is what the wheel ships and what
`config generate` reads. `tests/test_cli.py` asserts the two copies are
byte-identical, and `tests/test_config.py` parses the root ones against the
loader, so neither copy can drift.

`config.yaml` is gitignored: it names real accounts and will later sit beside
the agent's credentials. Every key is documented in
[docs/CONFIG.md](docs/CONFIG.md).

Project metadata is declared in PEP 621's `[project]` table, with only the
package layout and the dev dependency group left under `[tool.poetry]`. This
is safe precisely because Poetry is pinned to the project venv: nothing has to
stay readable by the old system Poetry.

## 🐳 Setting up in a container instead

Everything above assumes the toolchain on your own machine. `docker/` holds a
development image that carries it instead -- Python, git, Poetry with
`poetry.lock` installed, and the `claude` CLI -- with your checkout mounted,
so the same `poetry run ...` commands work inside it unchanged:

```bash
cd docker && cp .env.example .env   # then set PRA_USER/PRA_UID/PRA_GID
mkdir -p claude && docker compose up -d
docker compose exec dev zsh
```

[DOCKER.md](DOCKER.md) is the whole of it, including the two things that are
not obvious: PID 1 has to be a real init or the suite kills the container, and
a git *worktree* checkout needs its main repository mounted as well.

## 🧪 Testing

Test files live in _tests_ and must follow the `test_*.py` naming convention.
To run the suite with coverage:

```bash
poetry run pytest --cov=src/pr_review_agent --cov-report=term-missing
```

The suite needs no network and spends no tokens: the trigger pipeline is pure
functions over fixtures, the poller tests drive `httpx.MockTransport`, and the
store tests write to `tmp_path`. There is therefore no excuse for skipping it
before claiming a change is done.

Async tests need no decorator — `asyncio_mode = "auto"` means an
`async def test_*` is collected and run on a fresh event loop.

## 🖥 The command line

Every command follows one grammar, `pr-review-agent <noun> <verb>`, with the
nouns listed in the order an operator meets them:

```text
pr-review-agent config generate [--output PATH] [--full] [--force]
pr-review-agent config validate [--config PATH]
pr-review-agent host   check    [--config PATH]
pr-review-agent daemon start    [--config PATH]
```

| Exit | Meaning |
| :-- | :-- |
| `0` | success |
| `1` | a `host check` check failed |
| `2` | usage error, including a bare `pr-review-agent` |
| `3` | unusable config, missing token, or a refusal to overwrite a file |

`3` is not `2` because Click owns `2` for usage errors; merging them would
make a mistyped command indistinguishable from a missing credential. A bare
`pr-review-agent` started the daemon before 0.14 and now exits `2` rather
than printing help and exiting `0` — a zero exit would turn an unmigrated
systemd unit into a restart loop that reports success on every pass.

`python -m pr_review_agent.daemon` and `python -m pr_review_agent.bootstrap`
were the pre-0.14 spellings and no longer work.

## 🚦 Host checks

Before the daemon runs on a host for the first time, confirm the host can
reach what it needs:

```bash
GITHUB_TOKEN=... poetry run pr-review-agent host check
```

It fetches the three watched endpoints with the same client the poller uses,
re-fetches one conditionally and insists on a `304`, and checks the route to
`api.anthropic.com`. Exit status is `0` when every check passes, `1` when one
fails and `3` when the token or the config file is missing.

The conditional check is the one worth running even where egress obviously
works: the whole rate-limit budget rests on conditional requests being free,
and a proxy that strips `ETag` turns every poll into a full `200` — silently,
and visibly only once the budget runs out mid-week.

The token is read from the environment, never from `config.yaml`, and is never
printed. `--config` points at a config file other than `./config.yaml`.

## 🔄 Running the daemon

```bash
GITHUB_TOKEN=... poetry run pr-review-agent daemon start
```

It polls on the adaptive interval, classifies what changed, and enqueues what
the classifier accepts. It claims nothing and calls no review engine, so it
cannot spend allowance — the queue fills and nothing drains it until the worker
and the first engine adapter land. The [budget governor](docs/BUDGET.md) is already in place ahead
of it, so the spending rails exist before anything can spend.

Same conventions as the host checks: `GITHUB_TOKEN` from the environment,
`--config` for a config file elsewhere, exit `3` when either is missing. Exit
is `0` on `SIGINT` or `SIGTERM`, which are handled rather than waited out — a
shutdown does not sit through the remainder of a 600 s idle interval.

`SIGHUP` re-reads `config.yaml` and adopts its `budget` section without a
restart, which is what makes `budget.enabled: false` an emergency brake. A
broken file is logged and the previous configuration kept. Only `budget` is
hot-swapped; see [docs/CONFIG.md](docs/CONFIG.md#-reload).

The SQLite file comes from `store.path` in `config.yaml`, default `state.db`.
The resolved absolute path is logged at startup: a relative path is resolved
against the working directory the daemon starts in, and pointing at the wrong
file costs the queue's memory of what has already been reviewed.

[docs/DAEMON.md](docs/DAEMON.md) has the ordering rules the loop has to keep
and the cold-start bound that stops a fresh database paying for the backlog.

## 🔍 Linting and formatting

```bash
poetry run ruff check .
poetry run ruff format --check .        # drop --check to reformat in place
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 \
  --disable=missing-function-docstring,missing-module-docstring
```

`src` currently scores 9.99/10. The single deduction is an `R0801`
(`duplicate-code`) on the five-line "load the config, print why not, exit 2"
preamble that `bootstrap.main` and `daemon.main` share. The substance of that
step already lives in `_startup.py`; what remains is the idiom of turning a
`StartupError` into an exit status, and removing it would mean either a union
return type or raising `SystemExit` — which would cost `main` the
returns-an-exit-code contract its tests rely on.

The test pass disables the docstring checks because a test's name is its
description; every other check still applies.

## 🔬 Type checking

```bash
poetry run pyright src tests
```

Pyright errors should be resolved before submitting a pull request.

## 📦 Building

```bash
poetry build            # produces dist/*.whl and dist/*.tar.gz
```

A local build ships README.md as it stands, with relative documentation
links. The release workflow does one thing more: it runs
`python scripts/pypi_readme.py` and builds from that rewrite, in which every
relative link is absolute and pinned to the tag being released. PyPI resolves
a relative target against `pypi.org`, so an unrewritten README is a
documentation table that leads nowhere — the state of every release up to
0.16.0. The rewrite happens in the runner's checkout only and is never
committed; `pyproject.toml`'s `readme` has to keep naming README.md, because
`poetry install` reads it too.

CI additionally rejects any direct-URL (`file://`, `git+`, `https://`)
dependency that leaked into the built metadata, since such a package cannot be
installed from an index. It then installs the wheel into a throwaway venv and
walks the documented first run — `--help`, then `config generate` and
`config validate` in an empty directory. A wheel can import perfectly while
shipping no command, which is what `[project.scripts]` being absent did for
twelve releases; and it can ship a command while omitting the config
templates the quickstart tells the operator to copy, which is what the
missing `include` did for thirteen. No unit test can see either: every test
reads the source tree, where both have always been present.

## 🤖 Continuous integration

_.github/workflows/python-ci.yml_ runs the same commands listed above, and
bootstraps Poetry into `.venv` exactly as the Setup section does.

- `test` runs the suite on Python 3.10, 3.11, 3.12, 3.13 and 3.14 on Ubuntu,
  plus 3.12 on macOS and Windows. The full version range is covered on one OS
  and the other two are spot-checked, because the package is pure Python: a
  per-OS difference is far likelier than a per-version one.
- `quality` runs formatting, ruff, pylint, pyright and coverage once, on
  Ubuntu and 3.12, since none of those results vary by platform.
- `build` verifies the lock file, builds the wheel and sdist, rejects
  direct-URL dependencies in the built metadata, and installs the wheel to
  check the command it ships actually runs.

Everything CI runs can be run locally with the same `poetry run ...` command,
which is deliberate: a CI failure should always be reproducible on a laptop.

## ✅ The local gate

Run all of it before claiming a change is done, and quote the result rather
than predicting it:

```bash
poetry run pytest --cov --cov-report=term-missing
poetry run ruff format --check . && poetry run ruff check .
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pyright src tests
poetry build
```
