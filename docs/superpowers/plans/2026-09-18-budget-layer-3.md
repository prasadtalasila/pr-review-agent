# Budget Layer 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce the per-run wall clock by refusing a configuration whose
timeout could outlive the queue lease, and record on every ledger row why the
run it describes stopped.

**Architecture:** Two independent changes to merged code. `EngineConfig.parse`
gains an upper bound on `timeout_seconds` taken from `queue.DEFAULT_LEASE`, so
an invariant `queue.py` only asserted in a comment becomes a startup error. A
sixth SQLite migration adds `ledger.stop_reason`, a new `budget.StopReason`
enum is threaded through `Governor.settle` as a required argument, and
`ReviewWorker` supplies it — distinguishing a run killed on the clock from one
whose engine fell over, which today are the same row.

**Tech Stack:** Python 3.10–3.14, SQLite via stdlib `sqlite3`, `pytest` with
`pytest-asyncio`, Poetry. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-18-budget-layer-3-design.md`

## Global Constraints

- **`CLAUDE.md` §5 applies in full.** This touches the spending rails. Nothing
  here widens what triggers a review, removes a cap, or relaxes a trust check.
  Every new bound gets a test pinning it.
- **No test may spend a token or touch the network.** Everything below is pure
  functions over fixtures, a `tmp_path` SQLite file, and the existing loopback
  git double.
- **The full local gate must pass before the work is claimed done:**
  `poetry run pytest`, `poetry run ruff check .`, `poetry run ruff format
  --check .`, `poetry run pylint src tests`, `poetry run pyright`. Quote the
  output; do not predict it. See `DEVELOPER.md`.
- **Settlement amounts and confidences do not change.** A timed-out run still
  settles at its full reservation with `usage_confidence: unavailable`. Only
  the new `stop_reason` column distinguishes it.
- **The bound is pinned against `queue.DEFAULT_LEASE`, never a literal
  `1800`.** Changing the lease must not leave the check behind.
- Line length 88 (`ruff` default). Docstrings on every public callable —
  `pylint` enforces it. Comments explain *why*, matching the density of the
  surrounding file.

## File Structure

| File | Responsibility | Task |
| :-- | :-- | :-- |
| `src/pr_review_agent/config.py` | Bound `engine.timeout_seconds` by the lease | 1 |
| `tests/test_config.py` | Pin that bound at, above and below the lease | 1 |
| `src/pr_review_agent/store.py` | Migration 6: `ledger.stop_reason` | 2 |
| `tests/test_store.py` | Migration applies to a v5 database and a fresh one | 2 |
| `src/pr_review_agent/budget.py` | `StopReason`; `settle` writes it; `preflight` passes `REFUSED` | 2 |
| `tests/test_budget.py` | `settle` records the reason; existing call sites updated | 2 |
| `tests/test_engine.py` | One `settle` call site updated | 2 |
| `src/pr_review_agent/worker.py` | `EngineError` carries a reason; `run_one` supplies one on every path | 3 |
| `tests/test_worker.py` | Each `StopReason` reached by the path that should produce it | 3 |
| `docs/BUDGET.md`, `docs/CONFIG.md`, `docs/ENGINE.md`, `docs/STORAGE.md`, `docs/ROADMAP.md` | Layer 3's real disposition | 4 |

---

### Task 1: The wall clock may not outlive the lease

**Files:**
- Modify: `src/pr_review_agent/config.py` (imports; `_timeout`, around line 470)
- Test: `tests/test_config.py` (engine section, around line 437)

**Interfaces:**
- Consumes: `pr_review_agent.queue.DEFAULT_LEASE` (`timedelta`, 30 minutes).
  `queue` imports only `store`, `triggers.models` and `_compat`, so importing
  it from `config` creates no cycle.
- Produces: nothing new. `EngineConfig.timeout_seconds` keeps its type and
  meaning; only the accepted range narrows.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`, after `test_a_non_positive_timeout_is_refused`:

```python
def test_a_timeout_at_or_above_the_lease_is_refused():
    """A run that can outlive its lease loses it to a second worker.

    ``queue.py`` documents ``DEFAULT_LEASE`` as "comfortably above the
    per-run wall-clock ceiling", and leases carry an expiry rather than a
    heartbeat because of it. This is that assumption, enforced.
    """
    lease = DEFAULT_LEASE.total_seconds()
    for value in (lease, lease + 1):
        data = {**VALID, "engine": {**ENGINE, "timeout_seconds": value}}
        with pytest.raises(ConfigError, match="below the queue lease"):
            Config.from_mapping(data)


def test_a_timeout_below_the_lease_loads():
    """Pinned against the lease itself, so changing it cannot orphan the check."""
    lease = DEFAULT_LEASE.total_seconds()
    data = {**VALID, "engine": {**ENGINE, "timeout_seconds": lease - 1}}
    assert Config.from_mapping(data).engine.timeout_seconds == lease - 1
```

Add the import at the top of `tests/test_config.py`, beside the existing
`pr_review_agent.config` import:

```python
from pr_review_agent.queue import DEFAULT_LEASE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_config.py -k lease -v`

Expected: FAIL. `test_a_timeout_at_or_above_the_lease_is_refused` fails with
`DID NOT RAISE ConfigError`; `test_a_timeout_below_the_lease_loads` passes
already (it is the regression guard, not the new behaviour).

- [ ] **Step 3: Add the bound**

In `src/pr_review_agent/config.py`, add to the imports beside the existing
`from .triggers.allowlist import ...`:

```python
from .queue import DEFAULT_LEASE
```

Replace `_timeout` entirely:

```python
def _timeout(data: dict) -> float:
    """Read the wall clock one review may not outlive.

    Bounded above by the queue lease. ``queue.py`` calls ``DEFAULT_LEASE``
    "comfortably above the per-run wall-clock ceiling the budget governor
    enforces", and its module docstring goes further: a lease carries an
    expiry rather than a heartbeat *because* a run has a ceiling, so "a lease
    renewal would be machinery for a case that cannot arise." An operator who
    sets an hour makes that case arise -- the lease lapses under a live
    worker, a second worker claims the same pull request and reserves against
    the same windows, and the first worker's ``settle`` returns ``False`` and
    discards a review that was paid for. Nothing warns; the invariant was a
    comment. This is it enforced.

    Strictly below, not equal: at exactly the lease the two expire together
    and which one wins is a scheduling race.
    """
    value = data.get("timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError("engine.timeout_seconds must be a positive number")
    lease = DEFAULT_LEASE.total_seconds()
    if value >= lease:
        raise ConfigError(
            f"engine.timeout_seconds ({value:g}) must be below the queue lease "
            f"({lease:g}s); a run that outlives its lease loses it mid-review"
        )
    return float(value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_config.py -v`

Expected: PASS, all of them. If `config.example.yaml` or
`config.minimal.example.yaml` carries a `timeout_seconds` at or above 1800 the
example-file tests will now fail — lower it to `900` and say so in the commit.

- [ ] **Step 5: Run the full suite**

Run: `poetry run pytest`

Expected: PASS. Watch for `tests/test_bootstrap.py` and `tests/test_daemon.py`,
which build configs of their own.

- [ ] **Step 6: Commit**

```bash
git add src/pr_review_agent/config.py tests/test_config.py
git commit -m "Refuse a review timeout that could outlive its queue lease"
```

---

### Task 2: The ledger records why a run stopped

**Files:**
- Modify: `src/pr_review_agent/store.py` (`_MIGRATIONS`, after line 107)
- Modify: `src/pr_review_agent/budget.py` (`StopReason`; `_SETTLE`; `settle`; `preflight`)
- Test: `tests/test_store.py`, `tests/test_budget.py`
- Modify: `tests/test_engine.py:246` (one `settle` call site)

**Interfaces:**
- Produces: `pr_review_agent.budget.StopReason`, a `StrEnum` with members
  `COMPLETED`, `TRUNCATED`, `FAILED`, `TIMEOUT`, `ENGINE_ERROR`, `REFUSED`,
  `INFRASTRUCTURE`.
- Produces: `Governor.settle(claim, usage, *, now, stop_reason, reviewed_lines=None) -> bool`.
  **`stop_reason` is required and keyword-only.** No default, because a
  default would be a reason nobody gave — the same argument `Capabilities`
  makes for having no default booleans. Task 3 consumes this signature.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/test_store.py`, after
`test_an_existing_database_adopts_the_reviewed_lines_column`:

```python
def test_an_existing_database_adopts_the_stop_reason_column(tmp_path):
    """A v5 store gains the column that says why a run ended.

    Rows written before it existed keep a NULL, which is honest: nothing
    recorded why they stopped, and inventing a reason for them would put
    fiction in the one table that is never pruned.
    """
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("ALTER TABLE ledger DROP COLUMN stop_reason")
        conn.execute(
            "INSERT INTO ledger (dedupe_key, owner, actor_id, mode, "
            "reserved_tokens, reserved_at) VALUES ('k', 'w', 1, 'full', 10, 'x')"
        )
        conn.execute("PRAGMA user_version = 5")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        with reopened.transaction() as conn:
            assert conn.execute("SELECT stop_reason FROM ledger").fetchone() == (None,)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `poetry run pytest tests/test_store.py -k stop_reason -v`

Expected: FAIL with `sqlite3.OperationalError: no such column: stop_reason`
on the `DROP COLUMN`.

- [ ] **Step 3: Add migration 6**

In `src/pr_review_agent/store.py`, append to `_MIGRATIONS` after the
`reviewed_lines` entry:

```python
    # Why a run stopped, distinct from how far its cost can be trusted. A
    # timed-out run and an unreadable envelope both settle `unavailable` at
    # the full reservation, so without this column an operator cannot tell
    # which control bound the run.
    """
    ALTER TABLE ledger ADD COLUMN stop_reason TEXT;
    """,
```

Update the module docstring's ledger paragraph only if it enumerates columns;
it does not today, so leave it.

- [ ] **Step 4: Run it to verify it passes**

Run: `poetry run pytest tests/test_store.py -v`

Expected: PASS. `SCHEMA_VERSION` is `len(_MIGRATIONS)`, so it becomes 6
without being edited.

- [ ] **Step 5: Write the failing settle test**

Add to `tests/test_budget.py`, near the other `settle` tests:

```python
def test_settle_records_why_the_run_stopped(store):
    """The reason is a column, not an inference from the confidence."""
    governor = Governor(store, budget())
    claim = admitted(store, governor)

    assert governor.settle(
        claim,
        Usage(0, UsageConfidence.UNAVAILABLE, engine="claude"),
        now=NOON,
        stop_reason=StopReason.TIMEOUT,
    )

    with store.transaction() as conn:
        row = conn.execute(
            "SELECT stop_reason, usage_confidence FROM ledger"
        ).fetchone()
    assert row == (str(StopReason.TIMEOUT), str(UsageConfidence.UNAVAILABLE))


def test_a_refused_preflight_is_recorded_as_refused(store):
    """A free refusal is not a failure, and the ledger should not read as one."""
    governor = Governor(store, budget())
    claim = admitted(store, governor)

    assert governor.preflight(claim, 0, NOON) is False

    with store.transaction() as conn:
        (reason,) = conn.execute("SELECT stop_reason FROM ledger").fetchone()
    assert reason == str(StopReason.REFUSED)
```

`store` and `admitted` are the existing fixtures/helpers in that file; reuse
them rather than building new ones. If `admitted` is named differently, use
whatever the neighbouring tests use to get an admitted `Claim`.

Import `StopReason` alongside the existing `Governor, Mode, Usage,
UsageConfidence` import.

- [ ] **Step 6: Run it to verify it fails**

Run: `poetry run pytest tests/test_budget.py -k stop -v`

Expected: FAIL with `TypeError: settle() got an unexpected keyword argument
'stop_reason'`.

- [ ] **Step 7: Add `StopReason` and write it on settle**

In `src/pr_review_agent/budget.py`, add after the `UsageConfidence` class:

```python
class StopReason(StrEnum):
    """Why a run stopped, as recorded on its ledger row.

    ``UsageConfidence`` says how far the recorded cost can be trusted; this
    says what happened. They are different questions, and collapsing them is
    why a run killed on the wall clock and a run whose envelope would not
    parse were previously the same row -- both ``unavailable``, both charged
    the full reservation, and nothing to say which control had bound them.

    A bounded set rather than free text, because the column is read by an
    operator and, later, by the circuit breaker: ``GROUP BY stop_reason`` has
    to mean something.
    """

    COMPLETED = "completed"
    TRUNCATED = "truncated"
    FAILED = "failed"
    #: Killed by ``engine.timeout_seconds``. The only per-run ceiling the
    #: agent actually enforces, so it is worth being able to count.
    TIMEOUT = "timeout"
    ENGINE_ERROR = "engine_error"
    #: The pre-flight estimate refused the run. Settles at zero: no engine
    #: ran, so nothing was spent.
    REFUSED = "refused"
    #: GitHub or the workspace failed before the engine started.
    INFRASTRUCTURE = "infrastructure"
```

Change `_SETTLE`:

```python
_SETTLE = """
UPDATE ledger
SET used_tokens = :used, usage_confidence = :confidence,
    engine = :engine, model = :model, reviewed_lines = :lines,
    stop_reason = :reason, settled_at = :now
WHERE dedupe_key = :key AND owner = :owner AND settled_at IS NULL
"""
```

Change `settle`'s signature and body — add the parameter after `now`, and add
one paragraph to the docstring:

```python
    def settle(
        self,
        claim: Claim,
        usage: Usage,
        *,
        now: datetime,
        stop_reason: StopReason,
        reviewed_lines: int | None = None,
    ) -> bool:
```

Append to that docstring, after the `reviewed_lines` paragraph:

```
        ``stop_reason`` is required and has no default. A default would be a
        reason nobody gave, and the column exists precisely to remove that
        ambiguity -- a NULL here would mean the same thing the column was
        added to stop meaning: "something happened".
```

And in the parameter dict, add `"reason": str(stop_reason),`.

In `preflight`, pass the reason on the existing `self.settle(...)` call:

```python
        self.settle(
            claim,
            Usage(tokens=0, confidence=UsageConfidence.EXACT),
            now=now,
            stop_reason=StopReason.REFUSED,
        )
```

- [ ] **Step 8: Update the remaining `settle` call sites**

`stop_reason` is now required, so every caller must pass one. There are eight
in tests and one in `worker.py` (Task 3 handles that one). For the test call
sites, pass the reason the test is actually describing — `StopReason.COMPLETED`
for a clean run, which is all of them except where the test says otherwise:

- `tests/test_budget.py:153`, `:168`, `:203`, `:307`, `:467`, `:502`
- `tests/test_engine.py:246`

Do not add a default to silence them. The point of the change is that every
row carries a reason.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_budget.py tests/test_store.py tests/test_engine.py -v`

Expected: PASS.

- [ ] **Step 10: Confirm the windows are unchanged**

Run: `poetry run pytest tests/test_budget_concurrency.py -v`

Expected: PASS, untouched. The new column takes part in no window arithmetic;
if any of these fail, something changed that should not have.

- [ ] **Step 11: Commit**

```bash
git add src/pr_review_agent/store.py src/pr_review_agent/budget.py \
  tests/test_store.py tests/test_budget.py tests/test_engine.py
git commit -m "Record on every ledger row why its run stopped"
```

---

### Task 3: The worker supplies the reason on every path

**Files:**
- Modify: `src/pr_review_agent/worker.py` (`EngineError`, `run_one`, `_review`, `_settle_and_finish`)
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `budget.StopReason` and `Governor.settle(..., stop_reason=...)` from Task 2.
- Consumes: `pr_review_agent.engine.EngineTimeout`, already exported from
  `pr_review_agent.engine.__init__`.
- Produces: `worker.EngineError(message, reason)` — a second positional
  argument carrying a `StopReason`, readable as `exc.reason`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_worker.py`. First a stub that fails the way the CLI adapter
does, beside the existing `ExplodingEngine`:

```python
@dataclass
class TimingOutEngine:
    """An engine killed by its wall clock, as CliEngine.run kills one."""

    name: str = "timing-out"
    capabilities: Capabilities = FULL
    calls: int = 0

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Outlive the clock, having plausibly already spent tokens."""
        self.calls += 1
        raise EngineTimeout("timing-out exceeded 900.0s and was killed")
```

Then a helper beside `ledger_rows`:

```python
def stop_reasons(store: SqliteStore) -> list[str]:
    with store.transaction() as conn:
        return [
            row[0]
            for row in conn.execute("SELECT stop_reason FROM ledger ORDER BY id")
        ]
```

Then the tests:

```python
async def test_a_timed_out_run_is_distinguishable_from_a_crashed_one(wired):
    """The one ceiling the agent enforces is the one worth counting."""
    fixture = wired(engine=TimingOutEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.TIMEOUT)]
    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, _, _ = row
    # Unchanged by this work: a killed process printed nothing, so the run is
    # charged its ceiling at a confidence that says we did not measure it.
    assert (used, confidence) == (reserved, str(UsageConfidence.UNAVAILABLE))


async def test_an_engine_that_fell_over_reads_as_an_engine_error(wired):
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.ENGINE_ERROR)]


async def test_a_clean_review_reads_as_completed(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.COMPLETED)]


async def test_a_github_failure_before_the_engine_reads_as_infrastructure(wired):
    fixture = wired(client=client_returning(None, status=500))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.INFRASTRUCTURE)]
```

Add to the imports in that file:

```python
from pr_review_agent.budget import StopReason
from pr_review_agent.engine import EngineTimeout
```

(fold `StopReason` into the existing `pr_review_agent.budget` import line, and
`EngineTimeout` into the existing `pr_review_agent.engine` one.)

- [ ] **Step 2: Run them to verify they fail**

Run: `poetry run pytest tests/test_worker.py -k "stop or reads_as or distinguishable" -v`

Expected: FAIL — `TypeError: settle() missing 1 required keyword-only
argument: 'stop_reason'`, because Task 2 made it required and `worker.py` does
not pass it yet.

- [ ] **Step 3: Carry the reason on `EngineError`**

In `src/pr_review_agent/worker.py`, extend the existing `EngineError`. Keep
its docstring and append:

```python
class EngineError(RuntimeError):
    """A review engine failed, whatever it failed at.

    Every adapter is a foreign command-line tool, so the worker has no
    catalogue of what one can raise and no way to tell a timeout from a
    parse error from a bug inside it. All of them are the same fact here --
    this run produced no review -- and all of them are worth another attempt.
    Narrowing the boundary to this one call is what keeps a bug in the
    *worker* propagating to the supervisor instead of being retried three
    times in silence.

    One distinction survives the flattening, and only for the ledger: a run
    killed by its own wall clock is the agent's per-run ceiling doing its
    job, and a run whose tool fell over is not. The retry decision does not
    branch on it -- both are retried -- but an operator counting rows needs
    to know which control bound the run.
    """

    def __init__(self, message: str, reason: StopReason) -> None:
        super().__init__(message)
        self.reason = reason
```

Add the import: `from .budget import Governor, StopReason, Usage,
UsageConfidence` (extend the existing line), and extend the engine import to
`from .engine import EngineTimeout, Outcome, ReviewEngine, ReviewRequest,
ReviewResult`.

Add beside `WORKER_IDLE`:

```python
#: How a finished run's outcome reads on the ledger. Separate from
#: ``_finish_for``, which decides the queue row's fate: what happened and
#: what to do about it are different questions, and one dictionary answering
#: both would couple them.
_REASON_FOR = {
    Outcome.COMPLETED: StopReason.COMPLETED,
    Outcome.TRUNCATED: StopReason.TRUNCATED,
    Outcome.FAILED: StopReason.FAILED,
}
```

- [ ] **Step 4: Classify the failure in `_review`**

Replace `_review`:

```python
    async def _review(self, request: ReviewRequest) -> ReviewResult:
        """Run the engine, converting any failure of it into ``EngineError``."""
        try:
            return await self.engine.review(request)
        except EngineTimeout as exc:
            raise EngineError(
                f"{self.engine.name} outlived its wall clock: {exc}",
                StopReason.TIMEOUT,
            ) from exc
        except Exception as exc:  # the adapter is a foreign tool; see EngineError
            raise EngineError(
                f"{self.engine.name} failed: {exc}", StopReason.ENGINE_ERROR
            ) from exc
```

- [ ] **Step 5: Thread the reason through `run_one`**

In `run_one`, initialise the reason beside `usage` and `finish`:

```python
        usage = Usage(0, UsageConfidence.UNAVAILABLE, engine=self.engine.name)
        # Everything that can fail before the engine starts is infrastructure,
        # so it is the standing answer until something narrows it.
        reason = StopReason.INFRASTRUCTURE
        finish = self.queue.complete
```

In the success path, set it alongside the others:

```python
            usage, finish = result.usage, self._finish_for(result.outcome)
            reason = _REASON_FOR[result.outcome]
```

Split the engine failure out of the shared handler. `EngineError` shares no
ancestry with `GitHubClientError` or `WorkspaceError`, so the order of the two
`except` clauses does not affect which one catches — keep them adjacent and in
this order because they read as one taxonomy:

```python
        except EngineError as exc:
            logger.warning(
                "%s failed and will be retried", claim.trigger.dedupe_key, exc_info=True
            )
            reason = exc.reason
            finish = self.queue.release
        except (GitHubClientError, WorkspaceError):
            logger.warning(
                "%s failed and will be retried", claim.trigger.dedupe_key, exc_info=True
            )
            finish = self.queue.release
```

Pass it on the final call:

```python
        self._settle_and_finish(claim, usage, reason, finish)
```

And widen `_settle_and_finish`:

```python
    def _settle_and_finish(
        self,
        claim: Claim,
        usage: Usage,
        reason: StopReason,
        finish: Callable[[Claim], bool],
    ) -> None:
```

with `stop_reason=reason` added to the `self.governor.settle(...)` call.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_worker.py -v`

Expected: PASS, including the pre-existing tests — none of their assertions
touch `stop_reason`, and settlement amounts are unchanged.

- [ ] **Step 7: Run the full suite**

Run: `poetry run pytest`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/pr_review_agent/worker.py tests/test_worker.py
git commit -m "Tell a run killed on the clock from one whose engine fell over"
```

---

### Task 4: The documentation says what layer 3 actually is

**Files:**
- Modify: `docs/BUDGET.md` (layer table ~line 22; the layer 3 paragraph ~line 27; the test list ~line 426)
- Modify: `docs/CONFIG.md` (the `max_turns` rule ~line 29; the `budget` note ~line 153; the `engine` table ~line 218)
- Modify: `docs/ENGINE.md` ("What lands next")
- Modify: `docs/STORAGE.md` (the `ledger` schema)
- Modify: `docs/ROADMAP.md` (~lines 34, 83)

**Interfaces:**
- Consumes: everything from Tasks 1–3. This task adds no code.

- [ ] **Step 1: Correct the layer table in `docs/BUDGET.md`**

Change the layer 3 row to name what is enforced and what is not:

```markdown
| 3 | Per-run ceiling: max tokens, turn cap, wall-clock timeout | **done**, with one gap — see below |
```

Replace the paragraph beginning "Layer 3 needs a *running turn* to abort" with
an account of where each ceiling ended up: the wall clock is
`engine.timeout_seconds`, validated below the queue lease; the turn cap is
`queue.DEFAULT_MAX_ATTEMPTS`, enforced by the worker across attempts and by
the CLI's own `error_max_turns` within one; and the reservation is **not** yet
a ceiling, because every way to make it one assumes the engine reports tokens
and `Capabilities.usage_reporting` exists because one will not. Link the gap
to #20 and to the design note.

- [ ] **Step 2: Remove CONFIG.md's stale promises**

Two passages claim `max_turns` and `wall_clock_seconds` are coming:

- ~line 29: "The same rule is why `budget` carries `max_run_tokens` but not
  `max_turns`: the governor reserves against the first, and nothing yet reads
  the second." Rewrite: the turn cap is not a config key at all, because the
  worker's `max_attempts` bounds it.
- ~line 153: the paragraph saying both keys "arrive with the engine adapter".
  Delete it and say where each one went.

In the `engine` table, extend the `timeout_seconds` row to state the bound:
must be below the 30-minute queue lease, or the daemon refuses to start.

- [ ] **Step 3: Record the new column in `docs/STORAGE.md`**

Add `stop_reason` to the `ledger` schema listing with its seven values and one
line on why it is separate from `usage_confidence`: one says what happened,
the other how far the number can be trusted.

- [ ] **Step 4: Update "What lands next" in `docs/ENGINE.md`**

It currently says layer 3's per-run ceilings are still to come, "for which
`--max-budget-usd` and the turn caps are the levers". Replace with: the wall
clock landed and is validated against the lease; the token-denominated
ceiling is deferred to #20, and the design note records why `--max-budget-usd`
and a wall-clock-to-token conversion were both rejected.

- [ ] **Step 5: Update `docs/ROADMAP.md`**

Lines ~34 and ~83 both list layer 3's per-run enforcement as outstanding.
Mark the wall clock done and leave the reservation ceiling with #20.

- [ ] **Step 6: Add the new tests to BUDGET.md's verification list**

The list ending "Per-run ceilings terminating an over-budget review is layer
3, and lands with the engine adapter" is now wrong. Replace that sentence and
add the new guarantees:

- a review timeout at or above the queue lease fails startup, pinned against
  the lease rather than a literal;
- a run killed on the wall clock is distinguishable in the ledger from one
  whose engine fell over, and both still settle at the full reservation.

- [ ] **Step 7: Check every internal link still resolves**

Run: `poetry run pytest tests/ -k "doc or link" -v`

If the repository has no link test, check by hand that any anchor you renamed
(`#-the-rule-the-loader-follows` and the layer 3 heading in particular) is not
referenced from another page: `grep -rn "wall_clock_seconds\|max_turns" docs/
README.md`. Every remaining hit should be deliberate.

- [ ] **Step 8: Commit**

```bash
git add docs/
git commit -m "Say what layer 3 enforces, and what it does not"
```

---

### Task 5: Run the full gate

**Files:** none.

- [ ] **Step 1: Run every check `DEVELOPER.md` names**

```bash
poetry run pytest
poetry run ruff check .
poetry run ruff format --check .
poetry run pylint src tests
poetry run pyright
```

- [ ] **Step 2: Quote the output**

Paste the real result. `CLAUDE.md` §5: quote it, do not predict it. A failure
here is the task, not a footnote to it.

- [ ] **Step 3: Confirm no test spends anything**

Run: `grep -rn "pytest.mark.live" tests/`

Expected: only `tests/test_cli_engine_live.py`, unchanged. Nothing in this
work needed a live run.

- [ ] **Step 4: Amend issue #19 before claiming it closed**

Three of its five acceptance criteria change meaning, and a checklist quietly
left unticked is how a gap becomes folklore. Edit the issue body to:

- strike `max_turns`, naming `queue.DEFAULT_MAX_ATTEMPTS` and the CLI's own
  `error_max_turns` as where the cap actually lives;
- rename `budget.wall_clock_seconds` to `engine.timeout_seconds`, which is
  where #26 put it;
- mark "a run that would exceed its reservation is aborted, and settles at
  what it actually spent" as **not done**, linking the design note's argument
  and #20.

Do not silently tick a box this work did not earn. If the maintainer would
rather #19 stay open until the reservation is a real ceiling, that is their
call — ask before closing it.

---

## Deferred, and deliberately not in any task above

**`ReviewWorker` never passes `reviewed_lines` to `settle`.** Found while
reading `settle`'s signature for Task 2. `worker.py:217` calls
`self.governor.settle(claim, usage, now=_now())`, so every production
settlement writes `NULL` — and `_FIT_SAMPLE` requires `reviewed_lines IS NOT
NULL AND reviewed_lines > 0`, so the ledger can never reach
`MIN_FIT_SAMPLES` and `Governor._rate` will return
`DEFAULT_TOKENS_PER_LINE` forever. The pre-flight estimate's fit, which #25
built and tested, is unreachable in the running daemon.

It is a real defect in merged code and it is adjacent to this work — Task 3
edits that exact call. It is left alone anyway, per `CLAUDE.md` §3: it is not
this change's to fix, and folding it in would put a budget-behaviour change
inside a diff whose stated scope is ceilings and ledger metadata. It should
have its own issue, its own test, and its own review.
