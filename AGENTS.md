# Coding Assistant Guidelines

See `CLAUDE.md` for the behavioural rules that govern *how* a change is made,
and `DEVELOPER.md` for the commands that verify it.

## ROLE

The coding assistant acts as an expert Python developer working on a
locally-hosted PR review agent.

## GOALS

- Produce clean, readable, and maintainable code
- Keep functions below 25 lines of code and files below 250 (see
  RESTRICTIONS for what counts as a line)
- Follow recognised best practice and industry standards
- Provide clear explanations and documentation
- Support users in improving technical understanding

## PRINCIPLES

- **Clarity over cleverness**: code should remain easy to understand.
- **Modularity**: complex problems should be decomposed into manageable units.
- **Testing**: tests should accompany proposed code changes.
- **Performance**: efficiency is important, but readability takes priority.

## CODE STYLE

- Use consistent naming conventions.
- Follow language-specific style guides; here, `ruff format` is the
  authority on formatting and `ruff check` on import order and lint.
- Keep functions concise and focused.
- Use meaningful symbol names.
- Add comments only where logic is non-obvious. Prefer a docstring that
  explains *why* a rule exists over one that restates the signature.
- Public modules, classes and functions carry a docstring: `pylint` scores
  `src` at 9.97/10 against a 9.0 gate, and a missing docstring is a score
  regression.

## BEST PRACTICES

- **DRY (Don't Repeat Yourself)**: avoid unnecessary duplication.
- **SOLID principles**: apply object-oriented design principles where relevant.
- **Error handling**: handle potential errors in a controlled manner. Failures
  that affect correctness are raised, not swallowed; a deliberately tolerated
  failure carries a comment saying why.
- **Security**: account for security implications in all changes.
- **Version control**: use clear and descriptive commit messages.

## PROJECT LAYOUT

```text
src/pr_review_agent/          importable package (src layout)
  _compat.py                  stdlib shims for the oldest supported Python
  _startup.py                 token + config, shared by both entry points
  _subprocess.py              run a child under a clock; terminate, then kill
  _time.py                    the aware-UTC clock and the SQLite stamp format
  bootstrap.py                pre-flight egress checks for a new host
  breaker.py                  what a real usage limit teaches the guessed ones
  budget.py                   rolling windows, ladder, reserve-then-settle
  config/                     config.yaml loader, one module per section group
    __init__.py               the document: which sections exist, and Config
    _sections.py              ConfigError, and unknown-key rejection per section
    github_triggers.py        which repository, and whose requests
    budget.py                 every key that decides what may be spent
    engine.py                 which tool reviews, and under what clock
    excluded_paths.py         the built-in exclusions, generator globs from pr-agent
    runtime.py                store, workspace, worker, publish, logging
  daemon.py                   the poll-classify-enqueue loop and entry point
  description.py              what @claude describe produces, laid out as a comment
  logs.py                     one level and one format, resolved from three layers
  findings.py                 Finding and Severity: the type every layer handles
  numbering.py                finding numbers that survive a re-review
  pacing.py                   how often one pull request may be reviewed
  publisher.py                the 👀, the head re-check, one comment per PR
  report.py                   findings laid out as a report; nothing about posting
  queue.py                    claim protocol and per-pull-request leases
  comments.py                 the comment ids the agent posted, so it cannot answer itself
  runs.py                     what a paid review produced, so it can be re-posted
  sanitise.py                 engine prose made inert before it is posted
  store.py                    SQLite schema, watermarks, ETags, queue table
  worker.py                   claim, review, settle, close the row
  cli/
    __init__.py               the root group, the nouns, the exit codes
    _common.py                the shared --config option and startup handling
    cmd_config.py             config generate | validate
    cmd_daemon.py             daemon start
    cmd_host.py               host check
    cmd_service.py            service install -- place the systemd user unit
    cmd_skill.py              skill install -- place the review and description skills
  engine/
    models.py                 ReviewEngine protocol, Capabilities, request/result
    cli.py                    the subprocess boundary every CLI adapter shares
    claude.py                 the `claude` CLI adapter, the first engine that spends
    prompt.py                 what the reviewer is told; untrusted text fenced off
    describe.py               what the engine is told for @claude describe
    standards.py              review standards, read from the base ref
    fake.py                   an engine that spends nothing, for tests
  poller/
    endpoints.py              the three repo-wide request paths, and /pulls/{n}
    client.py                 async conditional GET, rate-limit handling
    etag_store.py             the ETagCache protocol and in-memory cache
    interval.py               adaptive poll delay
    payloads.py               raw GitHub dicts to trigger models
    pulls.py                  one pull request to PullRequestFacts
    poller.py                 one sweep across all three endpoints
  skills/                     the two skills the wheel ships
    __init__.py               one source for the prompt and for Claude Code
    review-report/            SKILL.md, references, assets, and its scripts:
      collect_context.py      header facts out of git
      render_report.py        findings.json to a report, via report.render
      check_report.py         a hand-written report against the contract
    pr-description/           the same shape, for @claude describe:
      collect_context.py      a copy of review-report's, byte for byte
      render_description.py   description.json to a description
      check_description.py    a hand-written description against the contract
  templates/                  the config templates and systemd units the wheel ships
    pr-review-agent.service   the single-repository user unit
    pr-review-agent@.service  the templated per-instance user unit
  triggers/
    models.py                 payload-shaped dataclasses; PayloadError
    allowlist.py              numeric-user-id membership
    mention.py                @claude in *prose* only
    classifier.py             PullRequest | Comment to Decision
  workspace/
    gitcmd.py                 the one hardened `git` invocation
    exclusions.py             configured path patterns to git pathspec arguments
    repo.py                   bare mirror, per-run worktree, diff, teardown
    since.py                  where an incremental round's diff starts, or that it is full
tests/                        pytest suite; a family per module under test
  conftest.py                 the loopback https git remote, and the plugin list
  *_harness.py                one family's doubles and fixtures, imported by it
  fixtures/                   GitHub payloads recorded by scripts/record_fixtures.py
.github/workflows/python-ci.yml   tests, lint, types, coverage: the gate
```

`tests/test_docs_layout.py` fails if a module is missing from this tree or
from the one in `DEVELOPER.md`. Add a module, add both lines in the same
commit.

- The supported range is **Python 3.10 - 3.14**, and CI runs all five. Code
  must therefore stay 3.10-compatible: a 3.11+ name goes behind a shim in
  `_compat.py` with a test pinning its behaviour, never an unguarded import.
- Poetry is installed **into the project venv**; never invoke a system-wide
  Poetry. See `DEVELOPER.md`.

- Dependencies are managed by Poetry in `pyproject.toml`; `poetry.lock` is
  committed and must be regenerated (`poetry lock`) in the same commit as any
  dependency change.
- Tests live in `tests/` and follow the `test_*.py` naming convention. A
  module whose suite outgrows the file limit is split into a family --
  `test_worker_claim.py`, `test_worker_publish.py` -- over one
  `*_harness.py` holding the doubles and fixtures they share. A harness
  defining a fixture is registered in `tests/conftest.py`'s
  `pytest_plugins`, so no test module imports a name it never calls.
- The trigger and poller suites are pure functions over fixtures: they must
  stay free of network access and must never call a real LLM. The engine
  suite runs against `FakeEngine` for the same reason — that is what the
  seam is for.

## RESTRICTIONS

- Explicit approval is required before introducing breaking changes.
- Unnecessary dependencies should not be added.
- Existing codebase patterns and conventions should be respected.
- Files should remain under 250 lines of code, and functions under 25.
  **Code** means what is left after blank lines, comments and docstrings are
  excluded. This repository explains itself at length on purpose, and a limit
  that counted prose would be met by deleting the explanations -- which is the
  opposite of what it is for. Measured this way the limit says what it means:
  this much behaviour in one place, and no more.
  `tests/test_module_size.py` enforces both on every file under `src/`,
  `tests/` and `scripts/`, so neither is a matter of judgement.
- Implementations should be tested when practical.
- Real credentials, tokens and account identifiers never enter the repository.
  `config.yaml` is gitignored; change `config.example.yaml` and
  `config.minimal.example.yaml` instead. A new key goes in the comprehensive
  one; the minimal one takes only required keys.
