# A noun-verb CLI, and a config template the install actually produces

Design for the 0.14 release. It fixes a shipped-and-broken quickstart and,
in the same breaking change, replaces the two `argparse` entry points with a
single `click` command tree following the
`pr-review-agent <noun> <verb>` grammar that the DTaaS CLI adopted in
[INTO-CPS-Association/DTaaS#1714](https://github.com/INTO-CPS-Association/DTaaS/issues/1714).

## The bug this starts from

The quickstart in `README.md` and `docs/index.md` tells an operator to
install the wheel and then run:

```bash
cp config.minimal.example.yaml config.yaml
```

That file is not there. `pyproject.toml` declares
`packages = [{ include = "pr_review_agent", from = "src" }]` and no `include`
for anything else, so `config.example.yaml` and
`config.minimal.example.yaml` — both at the repository root — are absent
from the wheel and the sdist. The instruction works only if you cloned the
repository, which the quickstart explicitly does not do. No code path
anywhere writes a config template, so there is nothing to fall back on.

A second, related gap: `[project.scripts]` ships one command,
`pr-review-agent = "pr_review_agent.daemon:main"`. `bootstrap.py` has a full
`main()` with the same `--config` contract, and `docs/DESIGN.md` instructs
operators to run the pre-flight before the daemon — but a wheel install
reaches it only through `python -m pr_review_agent.bootstrap`. The release
ships one of the two entry points it documents.

Both are the same class of failure as the twelve releases that shipped no
command at all: the wheel imports cleanly, and no unit test can see the
difference.

## Scope

In scope:

- ship the example configs inside the distribution;
- a `config generate` verb that writes one out;
- a `click` command tree with four commands under three nouns;
- a console script for the pre-flight checks, which has never had one;
- renumbered exit codes, because `click` reserves the number this project
  already uses;
- the documentation and CI changes the above force.

Out of scope: any change to what the daemon polls, classifies, enqueues,
spends or publishes. No behaviour behind the CLI moves.

## The command surface

```text
pr-review-agent config generate [--output PATH] [--full] [--force]
pr-review-agent config validate [--config PATH]
pr-review-agent host   check    [--config PATH]
pr-review-agent daemon start    [--config PATH]
```

Three nouns, listed in workflow order — `config`, `host`, `daemon` — via a
`WorkflowGroup` subclass of `click.Group` that overrides `list_commands`.
`--help` should read as the setup sequence rather than alphabetically, which
is the same reason DTaaS's root group does it.

`host` is a noun carrying one verb, and DTaaS's own rationale would question
that. It stays because the checks are about the *machine*: egress to
`api.github.com`, to `github.com`, to `api.anthropic.com`, and a git version
at or above 2.32. Folding them into `daemon check` would misdescribe what
fails when they fail.

### `config generate`

Writes `config.minimal.example.yaml` to `./config.yaml` by default. `--full`
writes the comprehensive 269-line commented template instead. `--output`
names a different destination.

It refuses to overwrite an existing file unless `--force` is given, exiting
3 with a message naming the path. A `config.yaml` names real GitHub accounts
and is gitignored, so a silent clobber destroys something with no copy
anywhere.

### `config validate`

Loads the file and reports what the loader made of it, or the
`ConfigError` it raised.

This verb forces one refactor. `_startup.startup()` today welds two
questions together: it reads `GITHUB_TOKEN` first and raises before it ever
opens the file, so a validate built on it would demand a token to check a
YAML file. It splits into `load_config(path)` and `require_token()`.
`config validate` calls only the first; the other three commands call both,
in that order, preserving today's behaviour for them.

### `host check` and `daemon start`

These are today's `bootstrap.main` and `daemon.main` bodies, unchanged in
behaviour. Both modules keep all their logic and lose their `main()` and
their `argparse` import; the click commands call `run_checks()` plus
`_report()`, and `run()`, directly.

Consequence: `python -m pr_review_agent.daemon` and
`python -m pr_review_agent.bootstrap` stop working. Both are documented —
`README.md`, `DEVELOPER.md` (twice), `docs/DESIGN.md` (twice),
`docs/DAEMON.md`, `docs/PUBLISHER.md` — and all of those become the new
spellings.

## Packaging the templates

Both templates are copied to `src/pr_review_agent/templates/` and added to
`[tool.poetry]` as `include` entries for both `sdist` and `wheel` formats,
mirroring the `src/templates/**/*` block in the DTaaS CLI's `pyproject.toml`.

The root-level copies **stay**. Removing them would churn the two GitHub
blob links in `docs/CONFIG.md` and break the copy-this-file instruction in
`DEVELOPER.md`, which is still the right instruction for a contributor
working from a clone. Two copies need a guard, so a test asserts the root
and packaged files are byte-identical; a change to one that is not made to
the other fails the suite. This is a deliberate trade: link stability and a
working clone workflow, paid for with one pinning test.

At runtime the packaged copies are read through
`importlib.resources.files("pr_review_agent") / "templates"`, which behaves
identically across 3.10 to 3.14 and therefore needs no `_compat.py` shim.

`tests/test_config.py`'s `EXAMPLES` constant continues to point at the
repository root, so its seven existing drift tests — the minimal one loads,
carries only required keys and no comments; the comprehensive one loads,
covers every key the loader accepts and states the real defaults — carry
over untouched. They remain the guard against a template drifting from the
loader.

## Exit codes

`click` exits 2 on a usage error. This project already uses 2 for "the token
or the config file is unusable" (`_startup.StartupError`, pinned by
`tests/test_daemon.py` and `tests/test_bootstrap.py`, documented in
`DEVELOPER.md`). Left alone, the two meanings merge and "you typed the
command wrong" becomes indistinguishable from "your token is missing".

The scheme becomes:

| Code | Meaning |
| :-- | :-- |
| 0 | success |
| 1 | a `host check` check failed |
| 2 | usage error — click's own, including a bare invocation |
| 3 | unusable config or missing token; refusing to overwrite a config file |

All four are pinned by tests. `DEVELOPER.md`'s bootstrap section documents
the old numbers and is updated.

## Bare invocation

`pr-review-agent` with no subcommand currently starts the daemon. Under the
grammar it cannot, and 0.14 is a breaking release, so it is not aliased for a
deprecation window.

It exits **2** with `did you mean 'pr-review-agent daemon start'?` rather
than printing help and exiting 0. A zero exit would turn a live systemd unit
into a restart loop that reports success on every pass — the failure mode
this project most consistently designs against. The only consumer of the old
spelling is a unit file a human edits once, and the error message tells them
what to edit it to.

## Guardrails

The project rule is that nothing may call a review engine outside the budget
governor, and that a change widening what triggers a review must say so and
pin the new bound. **This change widens nothing.** `config generate` and
`config validate` touch no network and construct no engine; `host check`
opens sockets but sends no API key and spends no tokens; `daemon start` is
today's daemon with today's governor.

Two tests pin it: the command tree contains exactly these four commands, and
only `daemon start` reaches `build_engine`. The token continues to be read
from the environment and never from `config.yaml`, in every command that
needs one.

## Testing

`click.testing.CliRunner` replaces the `main([...]) -> int` calls in
`tests/test_daemon.py` and `tests/test_bootstrap.py`. The assertions
themselves — exit 2, now 3, on a missing token and on an absent config file;
exit 0 and 1 for passing and failing checks — survive the rewrite.

New unit tests:

- `config generate` writes the minimal template to the default path;
- `--full` writes the comprehensive one instead;
- an existing destination is refused, and `--force` overrides it;
- `--output` honours a different path;
- **the generated file round-trips through `Config.load()`** — this is the
  regression test for the reported bug;
- `config validate` succeeds with no `GITHUB_TOKEN` in the environment;
- `config validate` exits 3 on a malformed file;
- the root and packaged templates are byte-identical;
- the command tree is exactly the four commands;
- bare invocation exits 2 and names `daemon start`.

CI already installs the built wheel into a throwaway venv and runs
`pr-review-agent --help`. That step gains
`pr-review-agent config generate` in a temporary directory followed by
`pr-review-agent config validate`. This is the only check in the project
that would have caught the reported bug: every unit test imports from the
source tree, where the templates have always been present.

## Documentation

- `README.md` and `docs/index.md`: the quickstart becomes
  `pr-review-agent config generate` instead of the `cp` that cannot work,
  and the two invocation examples gain `daemon start`.
- `docs/CONFIG.md`: the copy instruction becomes the new command; the blob
  links are unchanged, which is why the root copies stay.
- `DEVELOPER.md`: bootstrap section gains the console script and the new
  exit codes; the daemon section gains `daemon start`; the build section
  notes the widened wheel smoke test.
- `docs/DESIGN.md`, `docs/DAEMON.md`, `docs/PUBLISHER.md`: the `python -m`
  spellings become the new commands.
- `docs/ROADMAP.md`: record the CLI grammar as adopted.
- `pyproject.toml`: version 0.14.0.

## Files

New: `src/pr_review_agent/cli/__init__.py` (root group and `main`),
`cli/cmd_config.py`, `cli/cmd_host.py`, `cli/cmd_daemon.py`;
`src/pr_review_agent/templates/config.example.yaml` and
`config.minimal.example.yaml`; `tests/test_cli.py`.

Modified: `pyproject.toml` (click dependency, `include`, `project.scripts`,
version), `src/pr_review_agent/_startup.py` (the split),
`src/pr_review_agent/daemon.py` and `bootstrap.py` (drop `main` and
`argparse`), `tests/test_daemon.py` and `tests/test_bootstrap.py`
(`CliRunner`), and the documentation listed above.

The per-noun module naming mirrors the DTaaS CLI's `cmd_<noun>.py` layout
deliberately: the two projects share maintainers, and a reviewer moving
between them should not meet two idioms for the same grammar.
