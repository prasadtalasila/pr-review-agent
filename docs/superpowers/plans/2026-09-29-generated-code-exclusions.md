# Built-in Exclusions Out of the Config File, pr-agent's Generated-Code Globs In — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship pr-agent's per-generator globs as built-in exclusions, and make
`budget.excluded_paths` *add to* the built-in list rather than replace it, so
an operator names only what is special about their repository and the
sixty-odd standard patterns stop being echoed in every config file.

**Architecture:** The built-in list already lives in the package
(`DEFAULT_EXCLUDED_PATHS` in `config/budget.py`); the config file only
*repeats* it because setting the key replaces it. Three changes: (1) move the
list to its own module, `config/excluded_paths.py`, grouped by category with
pr-agent's generator globs added and attributed; (2) `BudgetConfig` gains a
boolean `default_exclusions` (default `true`) and `excluded_paths` becomes the
operator's *additions*, so the effective list is `BUILTIN + excluded_paths`
when the boolean is on and `excluded_paths` alone when it is off; (3) the
example config, `CONFIG.md` and `BUDGET.md` stop listing the built-ins and
describe the two keys instead.

**Tech Stack:** Python 3.10–3.14, pytest, the loopback git double in
`tests/conftest.py::git_remote` for the one test that must prove a glob
matches under git's own rules.

**Spec:** https://github.com/prasadtalasila/pr-review-agent/issues/122, plus
the request that motivated this plan: keep the bulk of the standard
directories out of the config file while still letting an operator add extra
patterns.

---

## What the issue establishes

`DEFAULT_EXCLUDED_PATHS` names lockfiles, vendored trees, three generated-code
patterns and minified bundles. pr-agent's `settings/generated_code_ignore.toml`
(MIT, commit `10bbd9a`) curates a per-generator list: protobuf in seven
languages, OpenAPI/Swagger stubs, GraphQL codegen, gRPC stubs in four
languages, Go generators. A generated file counts against `max_changed_lines`
and is shown to the engine while being close to worthless to review, so these
globs are the largest zero-token saving still on the table.

The issue's acceptance criteria:

- `test_config_budget.py` pins the new default list by value.
- `test_exclusions.py` proves at least one pattern per generator group
  excludes a representative path and keeps a sibling source file.
- The attribution line names pr-agent, the MIT licence and the upstream commit.

## The design change this plan adds, and why

Today `budget.excluded_paths` **replaces** the built-in list. That is why
`config.example.yaml` reprints all seventeen patterns: an operator who wants
to add one must copy the rest first or lose them. With pr-agent's globs the
list passes fifty entries, and reprinting it in every config file is exactly
the maintenance burden the request asks to remove.

So `excluded_paths` becomes **additive**. The one thing replacement bought —
"a repository that genuinely reviews its lockfiles exists", the rationale in
`budget.py` and both docs — is kept by a boolean, `default_exclusions: false`,
which drops the built-ins and leaves `excluded_paths` alone in force. One
boolean is the smallest knob that preserves the existing escape hatch; the
alternatives considered and not taken:

- **Keep replacement, ship the list in the package only.** No new key, but an
  operator adding one pattern must still reproduce sixty. Does not meet the
  request.
- **A second list key** (`extra_excluded_paths`) beside the replacing one. Two
  keys for one list, and every reader has to learn which wins.
- **Negation syntax** (`!**/poetry.lock`). More mechanism than the single
  known case justifies; can be added later without breaking the boolean.

Per `CLAUDE.md` §5: nothing here widens what triggers a review or what a run
may spend. Exclusions only *narrow* what the engine reads; turning the
built-ins off returns to what `excluded_paths: []` already allows today, and
the diff-size caps still bound the result.

**Compatibility.** An existing config that sets `excluded_paths` to the
reprinted seventeen keeps working: the effective list becomes the built-ins
plus seventeen duplicates, which git's pathspec treats as the same
exclusion. An existing config that sets `excluded_paths: []` to review
everything changes meaning — it now excludes the built-ins. Call this out in
the changelog and the `CONFIG.md` entry; it is the one operator-visible
break, and the fix is one line (`default_exclusions: false`).

## Dedup decision

Three upstream patterns are already present or subsumed: `**/*.pb.go` and
`**/*_pb2.py` are in the current list, and `**/*.generated.ts` is covered by
`**/*.generated.*`. The upstream group is copied **verbatim** so the
attribution is honest, and the three hand-written duplicates are removed
from the local generated group instead. `**/*.generated.*` stays because it
is broader than the upstream `.ts`-only pattern.

---

## Task 1: `config/excluded_paths.py` holds the built-in list

**Files:** `src/pr_review_agent/config/excluded_paths.py` (new),
`src/pr_review_agent/config/budget.py`, `src/pr_review_agent/config/__init__.py`.

- [ ] Create the module with four grouped tuples and one composed constant:
  `LOCKFILES`, `VENDORED`, `GENERATED`, `MINIFIED`, and
  `DEFAULT_EXCLUDED_PATHS = LOCKFILES + VENDORED + GENERATED + MINIFIED`.
  Keep the existing docstring's rationale (a default rather than a fixed
  list; reloadable on `SIGHUP`).
- [ ] `GENERATED` is the current `**/*.generated.*` followed by pr-agent's
  groups verbatim, each under a comment naming the generator (Protocol
  Buffers, OpenAPI / Swagger stubs, GraphQL codegen, RPC / gRPC generators,
  Go code generators), preceded by one attribution comment:
  `# The generator globs below are copied from pr-agent's
  settings/generated_code_ignore.toml at commit 10bbd9a (MIT licence,
  https://github.com/qodo-ai/pr-agent).`
- [ ] Remove `DEFAULT_EXCLUDED_PATHS` from `budget.py`; import it from the
  new module. `config/__init__.py` keeps re-exporting it under the same name
  so `tests/test_exclusions.py` and `tests/test_config_sections.py` import
  unchanged.
- [ ] Verify: `poetry run pytest tests/test_exclusions.py
  tests/test_config_sections.py` passes unchanged; `test_module_size.py`
  passes (the new module is well under 250 counted lines, and `budget.py`
  drops below its current 419).

## Task 2: the built-ins pass the same checks a config pattern must

**Files:** `tests/test_exclusions.py`.

- [ ] Add a test that every entry of `DEFAULT_EXCLUDED_PATHS` is a
  non-empty string not beginning with `:` — the issue's "must be a plain
  glob" requirement, stated once for the whole list.
- [ ] Add a test that the list has no duplicates, which pins the dedup
  decision above.
- [ ] Verify: both fail against a deliberately broken constant, pass against
  the real one.

## Task 3: `excluded_paths` adds to the built-ins; `default_exclusions` turns them off

**Files:** `src/pr_review_agent/config/budget.py`,
`tests/test_config_sections.py`, `tests/test_config_budget.py`.

- [ ] `BudgetConfig` gains `default_exclusions: bool = True`. `excluded_paths`
  keeps its type and its validation (`_excluded_paths`), and its default
  becomes `()` — it is now the operator's additions, not the effective list.
- [ ] Add a read-only property `effective_excluded_paths` returning
  `DEFAULT_EXCLUDED_PATHS + self.excluded_paths` when `default_exclusions` is
  true, else `self.excluded_paths`. `worker.py:547` passes the property
  instead of the field. Composition happens in one place so `adopt()`,
  `SIGHUP` reload and the dry-run summary all see the same answer.
- [ ] `_bool`-style reader for `default_exclusions` (refuse non-bool, the way
  `enabled` is read). Add the key to the section's known-keys list so the
  unknown-key check stays exact.
- [ ] Tests, in `test_config_sections.py`:
  - omitting both keys yields `effective_excluded_paths ==
    DEFAULT_EXCLUDED_PATHS`;
  - `excluded_paths: ['*.snap']` yields the built-ins **plus** `*.snap`
    (replaces `test_excluded_paths_can_be_replaced_outright`);
  - `default_exclusions: false` with a list yields that list alone, and with
    no list yields `()` — the "review your lockfiles" case;
  - `default_exclusions: "yes"` is refused with a message naming the key.
- [ ] `test_config_budget.py`: the adopt test asserts `excluded_paths ==
  ("**/local.lock",)` still (the field is local, not shared) and adds
  `effective_excluded_paths[-1] == "**/local.lock"`. Add the issue's pin: a
  test asserting `DEFAULT_EXCLUDED_PATHS` equals a literal tuple, so a
  change to the built-ins shows up as a diff to the test.
- [ ] Verify: `poetry run pytest tests/test_config_sections.py
  tests/test_config_budget.py tests/test_worker_loop.py
  tests/test_worker_refusals.py`. The worker tests build `budget(
  excluded_paths=("feature.py",))` and expect `feature.py` excluded; that
  still holds under additive semantics, so they should pass without edits.
  If `test_config_sections.py` crosses 250 counted lines, split the
  exclusion tests into `tests/test_config_exclusions.py`.

## Task 4: one pattern per generator group is proven against git

**Files:** `tests/test_exclusions.py` (or the new family file from Task 3),
`tests/conftest.py` if the fixture needs a parameter.

- [ ] Write a parametrised test over `(pattern, excluded_path,
  sibling_kept)`: protobuf `**/*.pb.go` → `api/v1/user.pb.go` vs
  `api/v1/user.go`; OpenAPI `**/__generated__/**` →
  `client/__generated__/api.ts` vs `client/api.ts`; Swagger
  `**/swagger.json` → `docs/swagger.json` vs `docs/openapi.md`; GraphQL
  `**/*.graphql.ts` → `src/types.graphql.ts` vs `src/types.ts`; gRPC
  `**/*_grpc.py` → `svc/user_grpc.py` vs `svc/user.py`; Go
  `**/*_gen.go` → `pkg/models_gen.go` vs `pkg/models.go`.
- [ ] Prove the match with git rather than `fnmatch`, because `**` means
  "any depth" only under git's `glob` magic: create a temp repository,
  `git add` both files, and assert `git ls-files` with
  `pathspec((pattern,))` appended lists the sibling and not the generated
  path. Reuse `workspace.gitcmd.run_git` so the same binary and environment
  the daemon uses are under test.
- [ ] Verify: the test fails when a pattern is misspelt (`**/*.pb.g`) and
  passes with the real list.

## Task 5: example config and docs describe two keys, not sixty patterns

**Files:** `src/pr_review_agent/templates/config.example.yaml`,
`config.example.yaml` (root copy — check whether it is generated from the
template or kept in step by a test; `test_config_loading.py:155` reads it),
`docs/CONFIG.md`, `docs/BUDGET.md`, `docs/WORKSPACE.md`, `CHANGELOG`/release
notes.

- [ ] Example config: replace the seventeen-line list with a commented
  `default_exclusions: true` and an `excluded_paths: []` whose comment says
  the built-ins are in the package (`config/excluded_paths.py`, four
  categories, generator globs from pr-agent), that this key **adds** to
  them, and that `default_exclusions: false` is how a repository reviews its
  lockfiles. `test_the_comprehensive_example_states_the_real_defaults`
  keeps passing because the shown values are once again the defaults.
- [ ] `CONFIG.md`: add the `default_exclusions` row; reword the
  `excluded_paths` row and the paragraph at line 238 from "replaces" to
  "adds to"; list the categories and name the generator groups rather than
  every glob; note the `[]` meaning change under a short "changed in" line.
- [ ] `BUDGET.md` "Path exclusions": the example block shows an addition and
  the opt-out; the "Setting the key replaces" paragraph becomes the
  additive rule; add the attribution sentence so the licence is visible in
  the docs, not only in source.
- [ ] `WORKSPACE.md:153` log excerpt: leave as is — it shows a log line, not
  a config file.
- [ ] Verify: `poetry run pytest tests/test_config_loading.py`; a grep for
  `REPLACES THE LIST` and `replaces the default list` returns nothing under
  `docs/` and the templates.

## Task 6: the full gate, then the version

- [ ] `poetry run pytest`, `ruff`, `pylint`, `pyright` as `DEVELOPER.md`
  describes. Quote the results in the pull request.
- [ ] Release note under the next minor version (a config key was added and
  the meaning of `excluded_paths: []` changed): the new key, the additive
  rule, the pr-agent attribution, and the one-line migration for anyone who
  set `excluded_paths: []`.

---

## Success criteria

1. `DEFAULT_EXCLUDED_PATHS` is defined in `config/excluded_paths.py`, pinned
   by value in a test, duplicate-free, and every entry is a plain glob.
2. A config that omits both keys excludes the built-ins; one that sets
   `excluded_paths` excludes the built-ins plus its own; one that sets
   `default_exclusions: false` excludes only its own.
3. One pattern per upstream generator group is shown, under git, to exclude a
   representative path and keep its sibling.
4. `config.example.yaml` no longer lists the built-in patterns and still
   loads to the defaults.
5. The attribution names pr-agent, MIT and commit `10bbd9a`, in source and
   in `BUDGET.md`.
