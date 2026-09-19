# Absolute `cache_dir`, and a Launch That Never Happened Costs Nothing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every review stop failing on a shipped-default configuration — `git -C <mirror> worktree add <relative-path>` resolves the path against the *mirror*, so the worktree lands somewhere the engine cannot find — and stop the resulting failure from draining the session allowance it provably never spent.

**Architecture:** Two changes, one failure story. First, `Workspace.__init__` resolves `cache_dir` to an absolute path once, at the boundary, the way `daemon.run` already resolves `store.path`; every path derived from it (`mirror`, `runs`, `run_path`) is then absolute and no `-C` can reinterpret it. Second, `worker._review` re-raises `EngineUnavailable` unwrapped — as it already does for `UsageLimited` — and `run_one` gains an arm that settles `Usage(0, UNAVAILABLE)` under a new `StopReason.ENGINE_UNAVAILABLE`, because a subprocess that failed at `create_subprocess_exec` has provably spent nothing.

**Tech Stack:** Python 3.10–3.14, asyncio, SQLite via `store.py`, pytest with `asyncio_mode = "auto"`, a loopback git double (`tests/conftest.py::git_remote`).

**Spec:** https://github.com/prasadtalasila/pr-review-agent/issues/42 — reproduced in full below under "What the issue establishes", so this plan is readable without it.

---

## What the issue establishes

Observed in the v0.13.0 live run: every review failed the instant the engine
was started, on two different run ids, and `rm state.db` in between changed
nothing.

```
FileNotFoundError: [Errno 2] No such file or directory: '.cache/repos/runs/e477ff08beeb'
pr_review_agent.engine.cli.EngineUnavailable: cannot run 'claude': [Errno 2] No such file or directory: '.cache/repos/runs/e477ff08beeb'
```

The `FileNotFoundError` is on the **cwd** `create_subprocess_exec` was handed,
not on the executable. `Workspace.checkout` runs

```python
await run_git("-C", str(self.mirror), "worktree", "add", "--detach", str(run_path), head_sha)
```

and `git -C <dir>` is equivalent to changing directory first, so every
relative path later on that command line resolves against the mirror.
`run_path` is `cache_dir / "runs" / <run_id>`, and `DEFAULT_CACHE_DIR` is
`.cache/repos` — relative, and relative is what `config.example.yaml`
documents. git therefore writes

```
.cache/repos/<owner>__<name>.git/.cache/repos/runs/<run_id>
```

while `Checkout.path` still says `.cache/repos/runs/<run_id>`, which the
engine resolves against the daemon's own working directory. Nothing is there.

Three consequences share the root cause:

1. **The run directory lands inside the mirror** — the one placement
   `workspace/repo.py`'s module docstring rules out.
2. **`runs/` exists but the run directory does not.** `self.runs.mkdir(...)`
   is Python and resolves against the process working directory.
3. **Nothing cleans it up.** `sweep()` does `shutil.rmtree(self.runs)`,
   resolved the same Python way, so it never sees the trees git wrote.
   `_teardown` has the same shape and the same blind spot.

**Why the suite does not catch it:** `tests/conftest.py::workspace` builds
every `Workspace` on `tmp_path`, which is absolute — and `-C` has no effect
on an absolute argument. Reproducing it needs a relative `cache_dir` *and* a
process working directory that is not the mirror, which is exactly the
shipped default and nothing the suite exercises.

**What it costs beyond the failure:** `run_one` raises `usage` to
`config.max_run_tokens` on the line *before* `_review`, so a run settles at
the reserved ceiling once the engine has been reached. A launch that failed
at `create_subprocess_exec` burned nothing and settles at the ceiling all the
same. The log shows the consequence:

```
WARNING pr_review_agent.budget the session window has 20000 tokens left, below the 60000 a run reserves: refusing mention:...
```

The daemon does not merely fail to review; it spends its whole session
allowance failing, then refuses everything until the window rolls.

## Decisions taken before planning

The issue leaves three things open. They are settled here and the tasks
assume them.

1. **The resolution happens in `Workspace.__init__`, not at the `worktree
   add` call site.** Absolutising only `run_path` fixes this one call and
   leaves the next relative path handed to a `git -C` to be found the same
   way — in production rather than in CI. The boundary is where a path stops
   being text and starts being a location.
2. **`EngineUnavailable` gets its own `StopReason.ENGINE_UNAVAILABLE`,** not
   a reused `INFRASTRUCTURE` or a zero-token `ENGINE_ERROR`. The column is
   read by an operator and by `GROUP BY stop_reason`; "the adapter could not
   be launched" and "the adapter fell over" are different operator actions —
   one is a misconfigured host, the other is a tool bug — and a zero-token
   `engine_error` row would sit next to ceiling-charged `engine_error` rows
   meaning something else. `stop_reason` is a plain `TEXT` column
   (`store.py:115`, migration 5) with no `CHECK` constraint, so a new member
   needs **no migration**.
3. **The queue row is still handed back with `queue.release`,** counting the
   attempt, as it is today. The `UsageLimited` arm's "drained nothing, so
   charge no attempt" argument does apply — but a usage limit clears itself
   when a window rolls, while a missing binary or an unwritable cache clears
   only when an operator acts. `release_unattempted` would hold every trigger
   in the queue forever on a misconfigured host. Bounded by `max_attempts` is
   the behaviour that eventually stops and says so. Only the settle amount
   changes; the retry taxonomy does not.

## Assumptions, stated rather than silently taken

- **`EngineUnavailable` means nothing was executed.** In `engine/cli.py` it
  is raised in exactly two places, both from an `OSError` out of
  `create_subprocess_exec`: `_start` (`cli.py:214`, the review path) and
  `version` (`cli.py:239`, which no review path calls). In both the process
  never came into being. Task 2 writes that contract into the class
  docstring, because from then on the *budget* depends on it: an adapter that
  raised `EngineUnavailable` after doing work would settle a real spend at
  zero.
- **The misleading message is part of the fix.** "cannot run `'claude'`" sent
  the first reading of this failure to the wrong subsystem. Task 2 puts the
  `cwd` in the message, which is cheap and traces directly to the issue's
  diagnosis.
- **The startup log gets no test of its own.** `daemon.run` is not exercised
  by `tests/test_daemon.py` (which builds a `Daemon` directly), and the
  existing `state database: %s` log line next to it has no test either. What
  *is* tested is the property the log reports: `Workspace(...).cache_dir` is
  absolute.
- **Worktrees already stranded inside the mirror are an operator note, not
  code.** `git worktree prune` will not remove a directory that still exists,
  so an operator hitting this deletes `.cache/repos/*.git/.cache/` by hand.
  It cannot recur once the path is absolute, so a one-off cleanup path would
  be dead code the day it merged (`CLAUDE.md` §2). It goes in the PR
  description and in `ROADMAP.md` §Known gaps.
- **`test_the_run_directory_is_not_inside_the_mirror` already exists**
  (`tests/test_workspace.py:55`) and passes today for the wrong reason. It is
  left alone; Task 1 adds the relative-`cache_dir` twin that can fail.
- **`cache_dir` is not reloaded on `SIGHUP`** (`CONFIG.md:319`), so resolving
  it once at construction cannot go stale.

## Global Constraints

- **Nothing may call a review engine outside the budget governor.** This
  branch adds no engine call site and moves none. (`CLAUDE.md` §5)
- **This branch widens nothing about spending.** No cap is removed, no
  trigger is added, nothing new is admitted. Its net effect is downward:
  today every admitted run settles at `max_run_tokens` having spent nothing.
  Task 2 adds the test that pins the new bound (`0`), as `CLAUDE.md` §5
  requires of any change to what the governor is charged.
- **Allowlisting stays on the numeric GitHub user id.** No trust check is
  touched.
- **No test reaches the network or spends a token.** Task 1 drives the
  loopback git double; Task 2 drives `FakeEngine` subclasses.
- Supported Python range is `>=3.10,<3.15`; `target-version = "py310"`. No
  3.11+ syntax. `enum.StrEnum` comes from `._compat`, never from `enum`.
- Line length 88 (ruff). Ruff lint rules: `E`, `F`, `I`, `UP`, `B`, `SIM`.
- Pyright runs over **`src` and `tests`** in `basic` mode.
- Pylint must score ≥ 9.0 on `src` and on `tests`.
- The full local gate before claiming done: `poetry run pytest`,
  `poetry run ruff check .`, `poetry run ruff format --check .`,
  `poetry run pylint src --rcfile=.pylintrc --fail-under=9.0`,
  `poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 --disable=missing-function-docstring,missing-module-docstring`,
  `poetry run pyright src tests`. Quote the result; never predict it.
- Every commit message ends with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **One branch, two commits.** Task 1 is the first, Task 2 the second. Each
  carries its own documentation changes.

## File Structure

| File | Responsibility | Task |
| :-- | :-- | :-- |
| `src/pr_review_agent/workspace/repo.py` | `Workspace.__init__` resolves `cache_dir`; the docstring says why | 1 |
| `src/pr_review_agent/daemon.py` | Log the resolved cache directory at startup, beside the store path | 1 |
| `tests/test_workspace.py` | The three tests the current suite cannot fail: checkout, placement, sweep — all on a relative `cache_dir` | 1 |
| `docs/WORKSPACE.md`, `docs/CONFIG.md` | `cache_dir` is resolved once at startup and logged | 1 |
| `src/pr_review_agent/budget.py` | `StopReason.ENGINE_UNAVAILABLE` | 2 |
| `src/pr_review_agent/worker.py` | `_review` re-raises `EngineUnavailable`; `run_one` settles it at zero | 2 |
| `src/pr_review_agent/engine/cli.py` | The `cwd` in the message; the "nothing was executed" contract in the docstring | 2 |
| `tests/test_worker.py` | An engine that never starts: settles zero, reads as `engine_unavailable`, is still retried | 2 |
| `docs/WORKER.md`, `docs/BUDGET.md`, `docs/ENGINE.md`, `docs/ROADMAP.md` | The failure table's third row; the contract; the operator note | 2 |

---

## Task 1: `cache_dir` is absolute from the boundary outward

**Files:**
- Modify: `src/pr_review_agent/workspace/repo.py:144-147` (`Workspace.__init__`)
- Modify: `src/pr_review_agent/daemon.py:369-377` (`run`)
- Test: `tests/test_workspace.py` (new fixture + three tests)
- Modify: `docs/WORKSPACE.md:30-43`, `docs/CONFIG.md:182-190`

**Interfaces:**
- Consumes: `tests/conftest.py::git_remote` (a `GitRemote` with `.repo`,
  `.base_url`, `.ca`, `.head_sha`, `.base_ref`), and `tests/test_workspace.py`'s
  module-level `facts(remote, **overrides)` helper and `CAPS` dict.
- Produces: `Workspace.cache_dir: Path` is now guaranteed absolute. No
  signature changes — `Workspace(repo, cache_dir: Path | str, base_url=...)`
  is unchanged, and `mirror`, `runs`, `remote_url`, `sweep`, `checkout`,
  `run_refs` keep their current types.

- [ ] **Step 1: Write the failing fixture and the first test**

Add to `tests/test_workspace.py`, after the `facts` helper. The module does
**not** import `Workspace` today — it only ever received the conftest fixture
— so extend its import block first (pyright runs over `tests`, so the return
annotation needs the name):

```python
from pr_review_agent.workspace import (
    DiffSize,
    PullRequestFacts,
    PullRequestTooLarge,
    Workspace,
)
```

```python
@pytest.fixture
def relative_workspace(git_remote, tmp_path, monkeypatch) -> Workspace:
    """A workspace on a *relative* cache_dir, which is what ships.

    The `workspace` fixture in conftest is built on `tmp_path`, which is
    absolute -- and `git -C` cannot reinterpret an absolute argument, which
    is precisely why the suite could not see #42. The literal below is
    `config.DEFAULT_CACHE_DIR`'s value written out rather than imported: if
    that default ever became absolute, this test must keep testing a
    relative one.
    """
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    monkeypatch.chdir(tmp_path)
    return Workspace(
        repo=git_remote.repo,
        cache_dir=".cache/repos",
        base_url=git_remote.base_url,
    )


async def test_a_relative_cache_dir_checks_out_where_it_says_it_did(
    relative_workspace, git_remote
):
    # The engine resolves `Checkout.path` against the daemon's working
    # directory; git resolved the same text against the mirror. #42 is the
    # gap between those two readings.
    async with relative_workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert checkout.path.is_dir()
        assert (checkout.path / "feature.py").read_text().startswith("def added")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `poetry run pytest tests/test_workspace.py::test_a_relative_cache_dir_checks_out_where_it_says_it_did -v`

Expected: FAIL on `assert checkout.path.is_dir()`. `worktree add` itself
*succeeds* — it writes the tree inside the mirror — so the failure is an
`AssertionError`, not an error from git. If instead the test errors out of
git, stop and re-read: the fixture is not reproducing the shipped shape.

- [ ] **Step 3: Write the two remaining failing tests**

```python
async def test_a_relative_cache_dir_keeps_the_run_directory_out_of_the_mirror(
    relative_workspace, git_remote
):
    # `git -C <mirror>` is a chdir, so a relative `worktree add` argument
    # lands under $GIT_DIR -- the one placement repo.py's docstring rules
    # out, and the one an absolute path cannot reach.
    async with relative_workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert relative_workspace.mirror not in checkout.path.parents
        assert not (relative_workspace.mirror / ".cache").exists()


async def test_the_sweep_clears_a_relative_cache_dirs_run_directory(
    relative_workspace, git_remote
):
    # `sweep` removes `self.runs` in Python and prunes worktrees in git. If
    # the two disagree about where a run directory is, a crashed run leaks
    # one forever.
    async with relative_workspace.checkout(facts(git_remote), **CAPS) as checkout:
        stranded = checkout.path
        (stranded / ".leaked").write_text("x")
    await relative_workspace.sweep()

    assert not stranded.exists()
    assert not list(relative_workspace.mirror.glob("worktrees/*"))
```

- [ ] **Step 4: Run all three to confirm the shape of the failure**

Run: `poetry run pytest tests/test_workspace.py -k relative -v`

Expected: the first two FAIL. The third may pass vacuously — teardown already
removed something — which is fine; it exists to stay passing after the fix,
not to fail before it. Note which ones failed, so Step 6 can be compared
against it.

- [ ] **Step 5: Make them pass**

In `src/pr_review_agent/workspace/repo.py`, `Workspace.__init__`:

```python
    def __init__(
        self, repo: str, cache_dir: Path | str, base_url: str = GITHUB_BASE
    ) -> None:
        self.repo = repo
        # Absolute, once, here. Every git command below is `git -C <mirror>`,
        # which is a chdir -- so a relative path on that argv is resolved
        # against the mirror and the worktree lands inside $GIT_DIR, while
        # `Checkout.path` still reads as relative to the daemon's own working
        # directory and the engine is handed a cwd that does not exist. The
        # shipped default is relative, so this is the configured case rather
        # than an exotic one.
        self.cache_dir = Path(cache_dir).resolve()
```

Also extend the module docstring's sibling-placement paragraph
(`repo.py:5-9`), which states the invariant this restores, with one sentence:

```
The placement is held by the path being absolute: ``git -C`` resolves a
relative argument against the mirror, which would put the run directory
inside the very ``$GIT_DIR`` this rules out.
```

- [ ] **Step 6: Run the three tests again**

Run: `poetry run pytest tests/test_workspace.py -k relative -v`
Expected: 3 PASSED.

- [ ] **Step 7: Run the whole workspace suite**

Run: `poetry run pytest tests/test_workspace.py -v`
Expected: all PASSED. `.resolve()` is a no-op on the absolute `tmp_path`
the existing fixture passes, so nothing there should move.

- [ ] **Step 8: Log the resolved path at startup**

In `src/pr_review_agent/daemon.py::run`, beside the existing store-path log:

```python
    workspace = Workspace(config.github.repo, config.workspace.cache_dir)
    # Same reason as the store path above: the configured default is
    # relative, so what it means depends on where the daemon was started.
    logger.info("workspace cache: %s", workspace.cache_dir)
```

- [ ] **Step 9: Update the two documents this changes**

In `docs/CONFIG.md`, the `workspace` table row for `cache_dir` — append to the
Meaning cell:

```
A relative path is resolved against the daemon's working directory **once, at startup**, and the absolute result is logged; it is not re-read on `SIGHUP`.
```

In `docs/WORKSPACE.md`, under "One mirror, a worktree per run", after the
"run directories are siblings of the mirror" paragraph:

```markdown
**`cache_dir` is made absolute before anything uses it.** Every git command
here is `git -C <mirror>`, which is a `chdir`: a relative path on that argv
is resolved against the mirror, not against the daemon's working directory.
A relative `cache_dir` therefore put each run directory inside `$GIT_DIR`
while `Checkout.path` still named a location the engine could not find, and
every review failed at the subprocess launch. Resolving at the boundary is
what keeps the two readings the same one.
```

- [ ] **Step 10: Run the full local gate**

```bash
poetry run pytest
poetry run ruff check .
poetry run ruff format --check .
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 --disable=missing-function-docstring,missing-module-docstring
poetry run pyright src tests
```

Expected: all pass. Quote the output; do not predict it.

- [ ] **Step 11: Commit**

```bash
git add src/pr_review_agent/workspace/repo.py src/pr_review_agent/daemon.py tests/test_workspace.py docs/WORKSPACE.md docs/CONFIG.md
git commit -m "$(cat <<'EOF'
fix: resolve workspace.cache_dir, so the worktree is where the engine looks

`git -C <mirror>` is a chdir, so the relative `run_path` on the `worktree
add` argv was resolved against the mirror: git wrote the tree under
$GIT_DIR while `Checkout.path` still named it relative to the daemon's own
working directory. With the shipped default `cache_dir: .cache/repos`,
every review failed at the subprocess launch with a FileNotFoundError on
its cwd.

Resolving once in `Workspace.__init__` makes `mirror`, `runs` and
`run_path` absolute, so no `-C` can reinterpret them, and restores the
sibling placement the module docstring already claimed. `sweep` and
`_teardown` had the same blind spot and are fixed by the same change.

The suite could not see this: every `Workspace` in it is built on an
absolute `tmp_path`, and `-C` has no effect on an absolute argument. The
new tests use a relative `cache_dir` and a working directory that is not
the mirror.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: A launch that never happened settles at zero

**Files:**
- Modify: `src/pr_review_agent/budget.py:172-186` (`StopReason`)
- Modify: `src/pr_review_agent/worker.py:59-66` (imports), `:291-298` (the
  `EngineError` arm), `:323-339` (`_review`)
- Modify: `src/pr_review_agent/engine/cli.py:44-45` (docstring), `:213-214`
  (the message)
- Test: `tests/test_worker.py`
- Modify: `docs/WORKER.md:90-103`, `docs/BUDGET.md:371-377` and `:590-595`,
  `docs/ENGINE.md:126-130` and `:264-268`, `docs/ROADMAP.md` §Known gaps

**Interfaces:**
- Consumes: `StopReason`, `Usage`, `UsageConfidence` from
  `pr_review_agent.budget`; `EngineUnavailable` from `pr_review_agent.engine`;
  `tests/test_worker.py`'s existing `wired(...)` fixture, `opened()`,
  `ledger_rows(store)`, `stop_reasons(store)`, `MAX_RUN_TOKENS`, and the
  `Capabilities`/`FULL` pair.
- Produces: `StopReason.ENGINE_UNAVAILABLE = "engine_unavailable"`. No
  signature changes: `worker._review` still returns `ReviewResult` and still
  raises `worker.EngineError` for everything it flattens.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_worker.py`. Put the engine double next to `ExplodingEngine`
(around `:167`) and the tests in the "failure: what it settles at" section,
immediately after `test_an_engine_failure_settles_the_full_reservation`.
Add `EngineUnavailable` to the existing `from pr_review_agent.engine import`
block.

```python
@dataclass
class UnstartableEngine:
    """An adapter whose subprocess never came into being.

    What `CliEngine._start` raises when `create_subprocess_exec` fails --
    a missing binary, or a cwd that is not there. No process existed, so
    the spend is zero and that is provable rather than assumed.
    """

    name: str = "unstartable"
    capabilities: Capabilities = FULL

    async def review(self, request: ReviewRequest) -> ReviewResult:
        del request
        raise EngineUnavailable("cannot run 'claude' in /nowhere: [Errno 2]")


async def test_an_engine_that_never_started_settles_at_zero(wired):
    """No process existed, so no tokens were spent. Provable, not assumed."""
    fixture = wired(engine=UnstartableEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, engine, _ = row
    assert reserved == MAX_RUN_TOKENS
    assert (used, confidence) == (0, str(UsageConfidence.UNAVAILABLE))
    assert engine == "unstartable"


async def test_an_engine_that_never_started_is_its_own_stop_reason(wired):
    """A misconfigured host and a tool that fell over are different rows."""
    fixture = wired(engine=UnstartableEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.ENGINE_UNAVAILABLE)]


async def test_an_engine_that_never_started_is_retried_with_its_attempt_spent(
    wired,
):
    """Bounded: a binary that is missing now is missing on the next claim."""
    fixture = wired(engine=UnstartableEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING
```

- [ ] **Step 2: Run them to verify they fail**

Run: `poetry run pytest tests/test_worker.py -k never_started -v`

Expected: the first two FAIL — `used == MAX_RUN_TOKENS` and
`stop_reasons == ['engine_error']` — plus an `AttributeError` on
`StopReason.ENGINE_UNAVAILABLE` in the second. The third PASSES already:
`release` is what happens today, and it is here to stay passing. That is the
whole bug, stated as three assertions.

- [ ] **Step 3: Add the stop reason**

In `src/pr_review_agent/budget.py`, inside `StopReason`, after
`ENGINE_ERROR`:

```python
    ENGINE_ERROR = "engine_error"
    #: The adapter's subprocess never started -- a missing binary, or a cwd
    #: that is not there. Settles at zero: no process existed, so nothing
    #: was spent, and that is provable in the way a killed run's spend is
    #: not. Kept apart from ``ENGINE_ERROR`` because the operator action
    #: differs: this one is the host's configuration, that one is the tool.
    ENGINE_UNAVAILABLE = "engine_unavailable"
```

No migration: `stop_reason` is a plain `TEXT` column with no `CHECK`
(`store.py:115`).

- [ ] **Step 4: Let `EngineUnavailable` reach `run_one` unflattened**

In `src/pr_review_agent/worker.py`, add `EngineUnavailable` to the
`from .engine import (...)` block, then in `_review`, beside the
`UsageLimited` re-raise:

```python
        except UsageLimited:
            # Not an engine failure to retry: it is the account's limit, and
            # `run_one` has an arm of its own for it.
            raise
        except EngineUnavailable:
            # The other failure the flattening must not swallow: the
            # subprocess never started, so `run_one` can settle it at a
            # provable zero rather than at the ceiling.
            raise
```

Order matters only against the bare `except Exception` at the end; both
re-raises must precede it.

- [ ] **Step 5: Settle it at zero in `run_one`**

In `src/pr_review_agent/worker.py::run_one`, between the `UsageLimited` arm
and the `EngineError` arm:

```python
        except EngineUnavailable:
            logger.warning(
                "%s could not start %s and will be retried",
                claim.trigger.dedupe_key,
                self.engine.name,
                exc_info=True,
            )
            # The reservation was raised to the ceiling on the line before
            # `_review`, because a run that reached the engine may have
            # spent anything. This one did not reach it: no process was
            # created, so the zero is provable and the ceiling would write
            # tokens that were never spent into all three rolling windows.
            usage = replace(usage, tokens=0)
            reason = StopReason.ENGINE_UNAVAILABLE
            # Still `release`, still counted. Unlike a usage limit, this does
            # not clear when a window rolls -- it clears when an operator
            # acts -- so the attempt bound is what eventually stops a
            # misconfigured host retrying every trigger forever.
            finish = self.queue.release
```

`replace(usage, tokens=0)` keeps the `engine=` name the initial `Usage`
carried, which
`test_an_engine_that_never_started_settles_at_zero` asserts.

- [ ] **Step 6: Run the three tests**

Run: `poetry run pytest tests/test_worker.py -k never_started -v`
Expected: 3 PASSED.

- [ ] **Step 7: Run the whole worker and budget suites**

Run: `poetry run pytest tests/test_worker.py tests/test_budget.py tests/test_budget_concurrency.py -v`

Expected: all PASSED. In particular
`test_an_engine_failure_settles_the_full_reservation` must still pass:
`ExplodingEngine` raises a bare `TimeoutError`, which is not
`EngineUnavailable`, so the ceiling rule is untouched for everything that
*did* start.

- [ ] **Step 8: Say what the message meant, and what the exception promises**

In `src/pr_review_agent/engine/cli.py`, `_start`:

```python
        except OSError as exc:
            # The cwd is named because it is the other thing that can be
            # missing here, and "cannot run 'claude'" sent the first reading
            # of exactly that failure to the wrong subsystem entirely.
            raise EngineUnavailable(
                f"cannot run {argv[0]!r} in {cwd}: {exc}"
            ) from exc
```

And the class docstring:

```python
class EngineUnavailable(EngineError):
    """The binary is missing, or the subprocess could not be started.

    An adapter may raise this **only when nothing was executed**. The worker
    settles it at a provable zero rather than at the reserved ceiling, so an
    adapter that raised it after doing work would write a real spend into
    the ledger as nothing -- see ``docs/WORKER.md``.
    """
```

- [ ] **Step 9: Update the documents**

`docs/WORKER.md`, the "What a failed run settles at" table — add a row after
the `before engine.review` row:

```markdown
| `EngineUnavailable` | `0`, `unavailable` | The subprocess never started, so no process existed to spend. Provable, like the row above it. |
```

and, after the paragraph ending "expressed by control flow rather than by a
flag that could disagree with reality", add:

```markdown
**`EngineUnavailable` is on the wrong side of that line, and is put back.**
It is raised where the engine call *starts* — `create_subprocess_exec`
refusing a missing binary or an absent cwd — so control flow places it after
the ceiling assignment while the fact it reports is the same one the rows
above it report: nothing ran. It settles at zero and reads as
`engine_unavailable`, apart from `engine_error` because the operator action
differs. It is still handed back with `release` and still counts its
attempt: a usage limit clears when a window rolls, but a missing binary
clears only when somebody fixes the host, and an uncounted retry would hold
every trigger in the queue forever.
```

`docs/BUDGET.md`, §"A caught failure settles at what is knowable" — extend the
last sentence:

```markdown
a failure before it settles at zero, a failure in or after it settles at the
full reservation, and the one failure *at* it that provably ran nothing —
`EngineUnavailable`, a subprocess that never started — settles at zero too.
```

and in the layer-3 test list near `:590`, after the "both still settle at the
full reservation" bullet:

```markdown
- an adapter whose subprocess never started settles at **zero** and reads as
  `engine_unavailable`, because the reservation exists to cover a spend that
  might have happened and this one could not have.
```

`docs/ENGINE.md` — after "a run that fails after the engine started is charged
its full reservation" (`:267`):

```markdown
The exception is `EngineUnavailable`, and it is a contract on the adapter
rather than a courtesy: raise it only when nothing was executed. The worker
settles it at zero, so an adapter raising it after doing work would record a
real spend as nothing.
```

`docs/ROADMAP.md` §Known gaps — add:

```markdown
- **Worktrees stranded by the relative-`cache_dir` bug are not cleaned up by
  code.** Before the fix, git wrote each run directory inside the mirror, and
  `git worktree prune` will not remove a directory that still exists. An
  operator who ran an affected version deletes `.cache/repos/*.git/.cache/`
  by hand, once. It cannot recur now that the path is absolute, which is why
  there is no code for it.
```

- [ ] **Step 10: Run the full local gate**

```bash
poetry run pytest
poetry run ruff check .
poetry run ruff format --check .
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 --disable=missing-function-docstring,missing-module-docstring
poetry run pyright src tests
```

Expected: all pass. Quote the output; do not predict it.

- [ ] **Step 11: Commit**

```bash
git add src/pr_review_agent/budget.py src/pr_review_agent/worker.py src/pr_review_agent/engine/cli.py tests/test_worker.py docs/WORKER.md docs/BUDGET.md docs/ENGINE.md docs/ROADMAP.md
git commit -m "$(cat <<'EOF'
fix: an adapter that never started settles at zero, not at the ceiling

`run_one` raises the reservation to `max_run_tokens` on the line before
`_review`, because a run that reached the engine may have spent anything.
`EngineUnavailable` is raised where that call starts -- OSError out of
`create_subprocess_exec` -- so no process ever existed and the spend is
known to be zero, the same knowability the `UsageLimited` arm already
argues from. Charging the ceiling wrote tokens that were never spent into
all three rolling windows, which is how a misconfigured host drained a
whole session allowance failing and then refused everything until the
window rolled.

It gets its own `StopReason.ENGINE_UNAVAILABLE`: a zero-token
`engine_error` row would sit beside ceiling-charged ones meaning something
else, and the operator action differs -- this is the host, that is the
tool. No migration; `stop_reason` is a plain TEXT column.

The retry taxonomy does not change. The row is still released and still
counts its attempt: unlike a usage limit, this clears when an operator
acts rather than when a window rolls.

`EngineUnavailable`'s message now names the cwd, which is the other thing
that can be missing there, and its docstring states the contract the
settle now depends on: raise it only when nothing was executed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review

**Spec coverage.** The issue's "Verification" list asks for four things:
a relative-`cache_dir` checkout whose `path` exists and is a git worktree
(Task 1 Step 1), a `sweep()` that removes a run directory created that way
(Task 1 Step 3), the sibling-placement invariant on a relative `cache_dir`
(Task 1 Step 3), and the full local gate (Task 1 Step 10, Task 2 Step 10).
Its "Proposed fix" — resolve in `Workspace.__init__`, log the absolute path
at startup — is Task 1 Steps 5 and 8. Its "Related, but a separate decision"
is Task 2, folded in at the user's request and settled in Decision 2 and
Decision 3. Its operator cleanup note is in the Task 2 `ROADMAP.md` change
and in the PR description.

**Not covered, deliberately:** no code removes already-stranded worktrees
(assumption 5); `daemon.run`'s log line has no test (assumption 3); the
pre-existing `test_the_run_directory_is_not_inside_the_mirror` is left as it
is (assumption 6).

**Types.** `StopReason.ENGINE_UNAVAILABLE` is named identically in
`budget.py`, `worker.py`, the test and all four documents.
`Workspace.__init__`'s signature is unchanged, so `daemon.py:377` and
`conftest.py:229` need no edit. `UnstartableEngine` implements the same
structural protocol as `ExplodingEngine` (`name`, `capabilities`, `async
review`), which is what `wired(engine=...)` accepts.
