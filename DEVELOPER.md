# PR Review Agent Developer Notes

This document describes how to set up, verify and build the project. The
source code lives in the _src/pr_review_agent_ directory and the test suite in
_tests_.

## 📦 Dependencies

The agent is written in Python (>= 3.11) and uses:

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

Layers depend downward only: `config` builds a `Classifier`, the poller
produces payloads for it, and nothing in `triggers/` imports `poller/`. That
is what keeps the trigger tests free of HTTP.

## ⚙️ Setup

```bash
pipx install poetry     # or: pip install poetry
poetry install          # create the venv and install runtime + dev deps
```

`poetry install` installs the `dev` group by default. To install only the
runtime dependencies (as the packaging job does), use `poetry install --only main`.

The project is configured (via _poetry.toml_) to create its virtual
environment in `.venv/` inside the repository, so editors and CI find the same
interpreter. Prefix commands with `poetry run`, or open a subshell with
`poetry env activate`.

Copy `config.example.yaml` to `config.yaml` before running the daemon.
`config.yaml` is gitignored: it names real accounts and will later sit beside
the agent's credentials.

Project metadata is declared in the `[tool.poetry]` form rather than PEP 621's
`[project]` table. That is deliberate: Poetry 1.8 (still the version shipped by
several distributions) cannot read `[project]` metadata at all and refuses to
run, whereas Poetry 2.x reads the `[tool.poetry]` form with only deprecation
warnings. Both the lock file and the build have been verified under 1.8 and
2.4. Switch to `[project]` once a 2.x floor is acceptable.

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

_.github/workflows/python-ci.yml_ runs the same commands listed above. Tests
run on Ubuntu, macOS and Windows; the lint, type-check and coverage steps run
on Ubuntu only, to avoid paying three times for a platform-independent result.
Everything CI runs can be run locally with the same `poetry run ...` command,
which is deliberate: a CI failure should always be reproducible on a laptop.
