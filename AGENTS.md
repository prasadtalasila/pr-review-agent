# Coding Assistant Guidelines

Adapted from the INTO-CPS Association's
[DTaaS](https://github.com/INTO-CPS-Association/DTaaS) assistant guidelines.
See `CLAUDE.md` for the behavioural rules that govern *how* a change is made,
and `DEVELOPER.md` for the commands that verify it.

## ROLE

The coding assistant acts as an expert Python developer working on a
locally-hosted PR review agent.

## GOALS

- Produce clean, readable, and maintainable code
- Keep functions below 25 lines and files below 250 lines
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
- Public modules, classes and functions carry a docstring: `pylint` is run at
  a 10.00/10 score on `src`, and a missing docstring is a score regression.

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
  bootstrap.py                pre-flight egress checks for a new host
  budget.py                   rolling windows, ladder, reserve-then-settle
  publisher.py                the 👀, the head re-check, one comment per PR
  runs.py                     what a paid review produced, so it can be re-posted
  config.py                   config.yaml loader and validation
  daemon.py                   the poll-classify-enqueue loop and entry point
  queue.py                    claim protocol and per-pull-request leases
  worker.py                   claim, review, settle, close the row
  store.py                    SQLite schema, watermarks, ETags, queue table
  triggers/                   allowlist, @mention parsing, classifier
  poller/                     GitHub REST polling, ETags, adaptive interval
  workspace/                  bare mirror, per-run worktree, diff, teardown
  engine/                     the ReviewEngine seam and a fake engine
tests/                        pytest suite, one test_*.py per module
.github/workflows/python-ci.yml   the single CI workflow
```

- The supported range is **Python 3.10 - 3.14**, and CI runs all five. Code
  must therefore stay 3.10-compatible: a 3.11+ name goes behind a shim in
  `_compat.py` with a test pinning its behaviour, never an unguarded import.
- Poetry is installed **into the project venv**; never invoke a system-wide
  Poetry. See `DEVELOPER.md`.

- Dependencies are managed by Poetry in `pyproject.toml`; `poetry.lock` is
  committed and must be regenerated (`poetry lock`) in the same commit as any
  dependency change.
- Tests live in `tests/` and follow the `test_*.py` naming convention.
- The trigger and poller suites are pure functions over fixtures: they must
  stay free of network access and must never call a real LLM. The engine
  suite runs against `FakeEngine` for the same reason — that is what the
  seam is for.

## RESTRICTIONS

- Explicit approval is required before introducing breaking changes.
- Unnecessary dependencies should not be added.
- Existing codebase patterns and conventions should be respected.
- Files should remain under 250 lines.
- Functions should remain under 25 lines.
- Implementations should be tested when practical.
- Real credentials, tokens and account identifiers never enter the repository.
  `config.yaml` is gitignored; change `config.example.yaml` and
  `config.minimal.example.yaml` instead. A new key goes in the comprehensive
  one; the minimal one takes only required keys.
