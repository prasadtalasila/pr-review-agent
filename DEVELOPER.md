# Developer guide

How to set up, verify and build the project. The source code lives in
_src/pr_review_agent_ and the test suite in _tests_.

This document is about the *toolchain*. For what the code does and why it is
arranged that way, start at [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md);
[CLAUDE.md](CLAUDE.md) holds the behavioural rules that govern how a change is
made, and [AGENTS.md](AGENTS.md) the coding conventions.

## 📦 Dependencies

The agent supports **Python 3.10 through 3.14** and uses:

- [PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation) — reads `config.yaml`.
  Loading goes through `yaml.safe_load` only.
- [httpx](https://www.python-httpx.org/) — the async HTTP client used by the
  poller for conditional (`If-None-Match`) GETs against the GitHub REST API.
  Its `MockTransport` is what lets the poller tests run with no network.
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

`sqlite3` is in the standard library, so the store adds no dependency.

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

Copy `config.example.yaml` to `config.yaml` before running the daemon.
`config.yaml` is gitignored: it names real accounts and will later sit beside
the agent's credentials. Every key is documented in
[docs/CONFIG.md](docs/CONFIG.md).

Project metadata is declared in PEP 621's `[project]` table, with only the
package layout and the dev dependency group left under `[tool.poetry]`. This
is safe precisely because Poetry is pinned to the project venv: nothing has to
stay readable by the old system Poetry.

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

## 🚦 Bootstrap checks

Before the daemon runs on a host for the first time, confirm the host can
reach what it needs:

```bash
GITHUB_TOKEN=... poetry run python -m pr_review_agent.bootstrap
```

It fetches the three watched endpoints with the same client the poller uses,
re-fetches one conditionally and insists on a `304`, and checks the route to
`api.anthropic.com`. Exit status is `0` when every check passes, `1` when one
fails and `2` when the token or the config file is missing.

The conditional check is the one worth running even where egress obviously
works: the whole rate-limit budget rests on conditional requests being free,
and a proxy that strips `ETag` turns every poll into a full `200` — silently,
and visibly only once the budget runs out mid-week.

The token is read from the environment, never from `config.yaml`, and is never
printed. `--config` points at a config file other than `./config.yaml`.

## 🔄 Running the daemon

```bash
GITHUB_TOKEN=... poetry run python -m pr_review_agent.daemon
```

It polls on the adaptive interval, classifies what changed, and enqueues what
the classifier accepts. It claims nothing and calls no review engine, so it
cannot spend allowance — the queue fills and nothing drains it until the engine
adapter lands. The [budget governor](docs/BUDGET.md) is already in place ahead
of it, so the spending rails exist before anything can spend.

Same conventions as the bootstrap checks: `GITHUB_TOKEN` from the environment,
`--config` for a config file elsewhere, exit `2` when either is missing. Exit
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

CI additionally rejects any direct-URL (`file://`, `git+`, `https://`)
dependency that leaked into the built metadata, since such a package cannot be
installed from an index.

## 🤖 Continuous integration

_.github/workflows/python-ci.yml_ runs the same commands listed above, and
bootstraps Poetry into `.venv` exactly as the Setup section does.

- `test` runs the suite on Python 3.10, 3.11, 3.12, 3.13 and 3.14 on Ubuntu,
  plus 3.12 on macOS and Windows. The full version range is covered on one OS
  and the other two are spot-checked, because the package is pure Python: a
  per-OS difference is far likelier than a per-version one.
- `quality` runs formatting, ruff, pylint, pyright and coverage once, on
  Ubuntu and 3.12, since none of those results vary by platform.
- `build` verifies the lock file, builds the wheel and sdist, and rejects
  direct-URL dependencies in the built metadata.

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
