# PR Review Agent Developer Notes

This document describes how to set up, verify and build the project. The
source code lives in the _src/pr_review_agent_ directory and the test suite in
_tests_.

## 📦 Dependencies

The agent supports **Python 3.10 through 3.14** and uses:

- [PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation) : reads
  `config.yaml`. Loading goes through `yaml.safe_load` only.
- [httpx](https://www.python-httpx.org/) : the HTTP client used by the poller
  for conditional (`If-None-Match`) GETs against the GitHub REST API. Its
  `MockTransport` is what lets the poller tests run with no network.
- [Poetry](https://python-poetry.org/docs/) : manages dependencies and builds
  the package. The configuration is _pyproject.toml_; new dependencies are
  added there and locked into _poetry.lock_.
- [pytest](https://docs.pytest.org/) with
  [pytest-cov](https://pytest-cov.readthedocs.io/) : the test suite.
- [Ruff](https://docs.astral.sh/ruff/) : formatting and fast linting.
- [Pylint](https://pylint.readthedocs.io/) : deeper static analysis, using the
  shared _.pylintrc_.
- [Pyright](https://github.com/microsoft/pyright) : static type checking,
  configured in _pyproject.toml_ under `[tool.pyright]`.

## 🏗️ Code Structure

The package has three layers:

- **Configuration layer** — _src/pr_review_agent/config.py_ loads and
  validates `config.yaml` into frozen dataclasses (`Config`, `GitHubConfig`,
  `TriggerConfig`). Unknown keys are rejected rather than ignored: a typo in a
  safety setting must fail at startup, not silently fall back to a default
  that spends tokens.

- **Trigger layer** — _src/pr_review_agent/triggers/_ decides whether an
  observed event starts a review. `models.py` holds the payload-shaped
  dataclasses, `allowlist.py` the numeric-user-id allowlist, `mention.py` the
  `@claude` parser (which ignores mentions inside fences, code spans and
  blockquotes), and `classifier.py` the single `Classifier` that turns a
  `PullRequest` or `Comment` into a `Decision`. Every rejection carries a
  reason code, so a "why was this not reviewed?" question is answerable from
  logs alone.

- **Poller layer** — _src/pr_review_agent/poller/_ is the only part that talks
  to GitHub. `endpoints.py` builds the three repo-wide request paths,
  `etag_store.py` caches the last ETag per path, `client.py` performs the
  conditional GET (parsing rate-limit headers and honouring `Retry-After`),
  `interval.py` implements the adaptive delay, and `poller.py` runs one cycle
  across all three endpoints.

Supporting Python 3.10 costs exactly one shim, in
_src/pr_review_agent/_compat.py_: `enum.StrEnum` arrived in 3.11. The
replacement is not the obvious `class StrEnum(str, Enum)` -- on 3.10 that
inherits `Enum.__str__`, so `str(member)` yields `"Endpoint.OPEN_PULLS"`
instead of `"open_pulls"`, and any interpolated log line or persisted dict key
would change meaning with the interpreter version. `_compat.py` delegates
`__str__` and `__format__` to `str` to restore the 3.11 behaviour, and
_tests/test_compat.py_ pins that parity. Those assertions are the reason the CI
matrix includes 3.10: it is the only job where the shim is imported at all.

Layers depend downward only: `config` builds a `Classifier`, the poller
produces payloads for it, and nothing in `triggers/` imports `poller/`. That
is what keeps the trigger tests free of HTTP.

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
the agent's credentials.

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
functions over fixtures, and the poller tests drive `httpx.MockTransport`.

## 🔍 Linting and Formatting

```bash
poetry run ruff check .
poetry run ruff format --check .        # drop --check to reformat in place
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 \
  --disable=missing-function-docstring,missing-module-docstring
```

`src` currently scores 10.00/10. The test pass disables the docstring checks
because a test's name is its description; every other check still applies.

## 🔬 Type Checking

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

## 🤖 Continuous Integration

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
