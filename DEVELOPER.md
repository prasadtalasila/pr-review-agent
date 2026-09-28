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
├── _subprocess.py     # run a child under a clock; terminate, then kill
├── _time.py           # the aware-UTC clock and the SQLite stamp format
├── _startup.py        # token and config, each loadable on its own
├── _subprocess.py     # a child process under a clock: terminate, then kill
├── _time.py           # the aware-UTC clock and the SQLite stamp format
├── bootstrap.py       # pre-flight egress checks for a new host
├── breaker.py         # what a real usage limit teaches the guessed ones
├── budget.py          # rolling windows, the ladder, reserve-then-settle
├── config/
│   ├── __init__.py    # the document: which sections exist, and `Config`
│   ├── _sections.py   # ConfigError, and unknown-key rejection per section
│   ├── github_triggers.py  # which repository, and whose requests
│   ├── budget.py      # every key that decides what may be spent
│   ├── engine.py      # which tool reviews, and under what clock
│   └── runtime.py     # store, workspace, worker, publish, logging
├── daemon.py          # the poll-classify-enqueue loop
├── logs.py            # one level and one format, resolved from three layers
├── numbering.py       # finding numbers that survive a re-review
├── pacing.py          # how often one pull request may be reviewed
├── publisher.py       # the 👀, the head re-check, one comment per review
├── queue.py           # claim protocol and per-pull-request leases
├── comments.py        # the ids the agent posted, so it cannot answer itself
├── runs.py            # what a paid review produced, so publishing can retry
├── sanitise.py        # engine prose made inert before it is posted
├── store.py           # SQLite: schema, watermarks, ETags, queue table
├── worker.py          # claim → review → settle → close the row
├── cli/
│   ├── __init__.py    # the root group, the nouns, the exit codes
│   ├── _common.py     # the shared --config option and startup handling
│   ├── cmd_config.py  # config generate | validate
│   ├── cmd_daemon.py  # daemon start
│   ├── cmd_host.py    # host check
│   └── cmd_service.py # service install — place the systemd user unit
├── engine/
│   ├── models.py      # ReviewEngine protocol, Capabilities, request/result
│   ├── cli.py         # the subprocess boundary every CLI adapter shares
│   ├── claude.py      # the `claude` CLI adapter: the first engine that spends
│   ├── prompt.py      # what the reviewer is told, and how untrusted text is fenced
│   ├── standards.py   # review standards, read from the base ref
│   └── fake.py        # an engine that spends nothing, for tests
├── poller/
│   ├── endpoints.py   # the three repo-wide request paths, and /pulls/{n}
│   ├── client.py      # async conditional GET, rate-limit handling
│   ├── etag_store.py  # the ETagCache protocol + in-memory cache
│   ├── interval.py    # adaptive poll delay
│   ├── payloads.py    # raw GitHub dicts → trigger models
│   ├── pulls.py       # one pull request → PullRequestFacts
│   └── poller.py      # one sweep across all three endpoints
├── templates/
│   ├── *.example.yaml # the two config templates the wheel ships
│   ├── pr-review-agent.service    # the single-repository user unit
│   └── pr-review-agent@.service   # the templated per-instance user unit
├── triggers/
│   ├── models.py      # payload-shaped dataclasses; PayloadError
│   ├── allowlist.py   # numeric-user-id membership
│   ├── mention.py     # @claude in *prose* only
│   └── classifier.py  # PullRequest | Comment → Decision
└── workspace/
    ├── gitcmd.py      # the one hardened `git` invocation
    ├── exclusions.py  # configured path patterns → git pathspec arguments
    └── repo.py        # bare mirror, per-run worktree, diff, teardown
```

`tests/test_docs_layout.py` walks `src/pr_review_agent` and fails if a module
is missing from this tree or from the one in _AGENTS.md_. The two drifted for
several releases before that test existed: the layout is the first thing a new
reader trusts, and a layout that omits the module doing the spending is worse
than none.

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
  used before 0.14, because a noun-verb grammar with grouped help is what
  Click gives for free and `argparse` only gives by hand.
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
byte-identical, and `tests/test_config_loading.py` parses the root ones against the
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
`async def test_*` is collected and run on a fresh event loop. A
`RuntimeWarning` is an error (`filterwarnings` in _pyproject.toml_): every one
this suite has produced was a coroutine left un-awaited, which is a teardown
that silently did not run.

### Families and harnesses

The same 250-line limit applies to a test file as to a module — it is
measured by `tests/test_module_size.py` over `src`, `tests` and `scripts` —
so a suite that outgrows it becomes a family: `test_worker_claim.py`,
`test_worker_failures.py`, `test_worker_publish.py` and so on, over a single
`worker_harness.py` holding the doubles, the constants and the `wired`
fixture they share.

A harness that defines a fixture is listed in `tests/conftest.py`'s
`pytest_plugins`, which is what makes `wired` or `store` available to its
family without every module importing a name it never calls itself. A
harness that defines only helpers is imported normally.

### Recorded fixtures

`tests/test_integration.py` replays real GitHub pages — a `/pulls` listing, a
comments page and one `/pulls/{n}` — through `MockTransport` and drives
`Daemon.run_once` over them. It is the acceptance criterion `docs/STATUS.md`
calls "integration tests against recorded GitHub API fixtures", and it needs
no network: the pages are committed under _tests/fixtures_.

Re-record them with `python scripts/record_fixtures.py`, which reads the
public API unauthenticated and documents the two normalisations it applies.

### The live engine test

`tests/test_cli_engine_live.py` runs a real `claude` and spends real tokens,
so `addopts = ["-m", "not live"]` deselects it. Opt in by hand:

```bash
poetry run pytest -m live tests/test_cli_engine_live.py
```

No workflow runs it: a scheduled job that spends tokens is a bill nobody
asked for on the day it fires. It is run by hand, when the adapter or the
CLI it drives has changed, and what it buys is the one thing the stubbed
suite cannot see — that the envelope the real binary prints is still the one
the parser reads.

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

It polls on the adaptive interval, classifies what changed, enqueues what the
classifier accepts, and **drains that queue**: the worker claims a row, checks
out the pull request, calls the configured review engine and posts the result.

**This spends real allowance, and posts under a real account.** The engine
adapter runs the `claude` CLI against a metered login, and a write-scoped
token is what the publisher posts with. Two settings sit in front of that, and
they are not interchangeable:

- **`budget.enabled: false` is the brake.** The governor refuses every claim
  while it is false, so nothing is reviewed and nothing is spent.
- **`publish.dry_run: true` is not.** The whole pipeline runs and the comment
  is logged instead of posted, so it costs exactly what a real review costs —
  the engine has already run by the time the publisher is asked. Use it to see
  what the agent would say, not to run it for free.

Both are re-read on `SIGHUP`. The [budget governor](docs/BUDGET.md) has the
rest of the rails: rolling windows, the ladder, reserve-then-settle.

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
poetry run ruff check src tests scripts
poetry run ruff format --check src tests scripts   # drop --check to rewrite
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 \
  --disable=missing-function-docstring,missing-module-docstring
```

**Ruff is pointed at `src tests scripts`, not at `.`**, and CI names the same
three. Those are the trees this project owns, and a gate should say what it
checks: `ruff format` rewrites whatever it is handed, so a bare `.` would have
formatted a future committed helper to this project's rules without anyone
deciding that. `extend-exclude` in _pyproject.toml_ keeps a bare `ruff check .`
in agreement for the common cases — a local `.claude/`, a built `site/`,
`dist/` — so the habit of typing `.` does not bury you in errors from files the
repository does not own.

`src` currently scores 9.98/10 and `tests` 9.46/10 under pylint 4.0, both well
above the 9.0 gate. Every remaining deduction in `src` is `R0902`
(too-many-instance-attributes) on the config, the daemon and the two trigger
models, plus `R0913` on the publisher. None is worth the indirection that
would silence it: a dataclass with seven fields instead of nine buys nothing.

The two that *were* worth it are gone. `R0801` (`duplicate-code`) sat on the
terminate-then-kill helper until `engine/cli.py` and `workspace/gitcmd.py`
came to share `_subprocess.py`; `R0914`/`R0915` sat on `ReviewWorker.run_one`
until its exception taxonomy became `classify_failure`. Both were the size
heuristic pointing at something real, which is the case the rule exists for.

Quote the score from the pinned pylint rather than from whatever is on your
`PATH`. The two disagree: 3.3 rated this same tree 9.95, and a number from the
wrong version reads as a regression that is not one.

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
walks the documented first run — `--help`, then `config generate`, a
`config validate` that must **fail** because the shipped template still says
`owner/name`, and a second one that must pass once the repository is filled
in. A wheel can import perfectly while
shipping no command, which is what `[project.scripts]` being absent did for
twelve releases; and it can ship a command while omitting the config
templates the quickstart tells the operator to copy, which is what the
missing `include` did for thirteen. No unit test can see either: every test
reads the source tree, where both have always been present.

## 🤖 Continuous integration

_.github/workflows/python-ci.yml_ runs the same commands listed above, and
bootstraps Poetry into `.venv` exactly as the Setup section does. It is the
gate; the other two workflows are _docs.yml_, which publishes the site, and
_release.yml_, which reacts to a version tag.

- `test` runs the suite on Python 3.10, 3.11, 3.12, 3.13 and 3.14 on Ubuntu,
  plus 3.12 on macOS and Windows. The full version range is covered on one OS
  and the other two are spot-checked, because the package is pure Python: a
  per-OS difference is far likelier than a per-version one.
- `quality` runs formatting, ruff, pylint, pyright and coverage once, on
  Ubuntu and 3.12, since none of those results vary by platform.
- `build` verifies the lock file, builds the wheel and sdist, rejects
  direct-URL dependencies in the built metadata, and installs the wheel to
  check the command it ships actually runs.
- `docker` builds _docker/Dockerfile_, runs _docker/entrypoint.sh_ against the
  checkout, and proves the package the entrypoint links is importable and its
  console script runs. It does not re-run the gate inside the image — `test`
  and `quality` already did that on the host — so what it buys is that a build
  break or a broken entrypoint fails a pull request instead of being found by
  the next person who needs the container. Layers are cached between runs;
  [DOCKER.md](DOCKER.md#-verify-the-container) has the by-hand checks that
  stay by hand.

`quality` also runs [pip-audit](https://pypi.org/project/pip-audit/) over the
dependencies, and a known vulnerability **fails the build**. The agent runs a
metered credential and posts under a real account, so a vulnerable `httpx`,
`PyYAML` or `click` should stop a merge rather than wait for somebody to
notice. Two details are deliberate:

- **The lock file is what gets audited**, frozen out of the venv CI just
  installed rather than re-resolved. That answers "is what we ran vulnerable",
  not "would a fresh resolution be" — which are different questions, and only
  the first one is about this build.
- **pip-audit runs from a venv of its own.** Installed alongside the project
  it would audit its own dependency tree as well, and `--skip-editable` still
  fails `--strict` on this package.

To reproduce a CI audit locally:

```bash
poetry run python -m pip freeze --exclude-editable > audit-requirements.txt
.venv/bin/python -m venv .audit
.audit/bin/python -m pip install --upgrade pip pip-audit
.audit/bin/pip-audit --strict --desc --requirement audit-requirements.txt
```

If a finding genuinely cannot be fixed, `--ignore-vuln <ID>` is the escape
hatch and the justification belongs in the workflow beside it — not in a
commit message where nobody rereads it.

## 🤖 Dependency updates

_.github/dependabot.yml_ opens the pull requests; CI decides. Nothing is
bumped automatically, and that is the point — the same five checks a human
change goes through are what say whether an upgrade is safe.

Two ecosystems, weekly, because this repository has two kinds of pinned
dependency and they fail differently. Actions are pinned by commit SHA, which
is the right pin and also the one nothing renews; `poetry.lock` is refreshed
by hand and otherwise not at all. `pip` updates are grouped into **runtime**
and **dev** rather than one pull request: a runtime bump changes what the
daemon executes against a real token, a dev bump changes only what checks it,
and reviewing both in one diff makes the second hide the first.

Everything CI runs can be run locally with the same `poetry run ...` command,
which is deliberate: a CI failure should always be reproducible on a laptop.

## ✅ The local gate

Run all of it before claiming a change is done, and quote the result rather
than predicting it:

```bash
poetry run pytest --cov --cov-report=term-missing
poetry run ruff format --check src tests scripts
poetry run ruff check src tests scripts
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pyright src tests
poetry build
```

The dependency audit is not in that list because it needs the network and a
throwaway venv; run it when you touch _poetry.lock_, with the four commands
in [Continuous integration](#-continuous-integration).
