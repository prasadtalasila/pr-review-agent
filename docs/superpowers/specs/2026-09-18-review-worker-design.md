# The review worker: draining the queue through the governor

Design for issue
[#16](https://github.com/prasadtalasila/pr-review-agent/issues/16).

The daemon stops at `enqueue`. The backlog is visible and none of it has cost
anything, which every phase so far has been built to preserve. This change
builds the thing that drains it — and drains it onto `FakeEngine`, so the
whole pipeline runs end to end and still spends nothing.

**That is the point of doing it now.** This is the last moment at which
claim → run → settle → finish can be verified for free. Once an adapter
exists, every exercise of this path costs allowance.

## Scope

In: a `worker` module that claims through the governor, resolves the pull
request, checks it out, runs an engine, settles and finishes the row; a
supervisor that keeps it alive; the queue verb a permanent failure needs; a
config key for how many workers run.

Out, explicitly: any adapter that spends anything; the publisher. Findings
are logged and dropped — there is nowhere to post them, and inventing the
publisher's data model here would be designing it before it has a caller.
The `head_sha` re-check belongs with publishing and is not attempted.

`CLAUDE.md` §5's spending rule **is** engaged, for the first time: this is
the change that lets something call a review engine. Two bounds answer it —
every call is behind `Governor.admit`, and `worker.count` is capped at 4 with
the default and the cap pinned by tests.

## The shape

`src/pr_review_agent/worker.py`. It imports `queue`, `budget`, `workspace`,
`engine` and `poller.pulls`; nothing imports it but `daemon.py`.

```python
@dataclass
class ReviewWorker:
    queue: ReviewQueue
    governor: Governor
    workspace: Workspace
    engine: ReviewEngine
    client: GitHubClient
    endpoints: RepoEndpoints
    owner: str
    completed: int = 0
```

**No field holds anything from a run.** Everything a review needs is derived
from its claim and discarded with it. That is what makes "one pull request
cannot pollute the next" a property of the code rather than a hope, and it is
also what lets the supervisor treat re-entering the loop as spawning a fresh
worker.

`completed` is the one exception, and it is not run state: it counts finished
runs so the supervisor can tell a worker that is making progress from one
that is crash-looping.

The size caps are read off `governor.config` **on each run**, never
snapshotted at construction. `Workspace.checkout` already takes them as
arguments for exactly this reason: `budget` is reloaded on `SIGHUP`, and a
holder of a stale snapshot would ignore a tightened cap — the failure the
reload mechanism exists to prevent.

## One run

```python
usage = Usage(0, UNAVAILABLE, engine=self.engine.name)
try:
    facts = await fetch_pull_request_facts(client, endpoints, pr_number)
    async with workspace.checkout(facts, **caps) as checkout:
        usage = replace(usage, tokens=governor.config.max_run_tokens)
        result = await engine.review(ReviewRequest(...))
    usage, finish = result.usage, queue.complete
except (PullRequestTooLarge, PayloadError):
    finish = queue.abandon
except (GitHubClientError, WorkspaceError):
    finish = queue.release
governor.settle(claim, usage, now=now)
finish(claim)
```

`PullRequestTooLarge` subclasses `WorkspaceError`, so its clause must come
first. A test pins that an oversized pull request abandons rather than
retries.

### What a failure settles at

The issue is explicit that `settle` must run even when the engine raises,
because a crashed run otherwise stays charged at its full reservation until
the window rolls. That is the deliberate behaviour for a *lost* worker; a
caught exception is not a lost worker.

But "settle at what was actually spent" is not answerable for a crashed
engine — a CLI adapter killed on timeout has spent real tokens and reports
none. So the rule is split on the one thing that is knowable:

| Where it failed | Settles at | Why |
| :-- | :-- | :-- |
| Before `engine.review` | 0, `unavailable` | Nothing reached an engine. Provable, not assumed. |
| In or after `engine.review` | the full reservation | Anything may have been spent. Same pessimism as a lost worker. |
| Success | `result.usage` | What the engine reported. |

The `usage` variable therefore takes exactly three values, and the assignment
that raises it to the full reservation sits on the line before the engine
call. "Did the engine start" is expressed by control flow, not by a flag that
could disagree with reality.

The pessimistic direction is the safe one for a spending control, and it is
the same argument `BUDGET.md` already makes for not granting an amnesty to a
crashed worker.

### Retryable and permanent

A failed run has two possible fates and they are not interchangeable.
`release` hands the row back as `pending` with its attempt already counted,
so three failures reach `abandoned`; `complete` marks it `done`, and because
the dedupe key is what makes "already reviewed" a fact, nothing will ever
revisit it.

| Failure | Fate | Why |
| :-- | :-- | :-- |
| `GitHubClientError` | `release` | 5xx, rate limit, network: transient by construction |
| `WorkspaceError` / `GitCommandError` | `release` | Fetch failures are network failures |
| engine raises | `release` | May succeed next time; bounded by `max_attempts` |
| `PullRequestTooLarge` | `abandon` | Deterministic on the same head. Two more attempts reach the same refusal, reserving allowance each time. |
| `PayloadError` | `abandon` | Deterministic |

This adds one verb to the queue:

```python
def abandon(self, claim: Claim) -> bool:
    """Give up on ``claim`` permanently; ``False`` if the lease is gone."""
    return self._finish(claim, QueueStatus.ABANDONED)
```

Owner-guarded, the same `_FINISH` statement as `complete` and `release`. It
exists rather than reusing `complete` because `done` means *reviewed* — a row
refused for its size is not a reviewed row, and an operator reading the table
should not have to guess which kind of `done` they are looking at.
`ABANDONED` was previously reachable only through `max_attempts` exhaustion
inside `claim`; this gives it a second, deliberate route.

### Ordering, and the lapsed lease

`settle` runs before the queue verb. Both are guarded on the owner, so a
worker whose lease lapsed and was re-claimed by another gets `False` from
`settle`, logs it, and does not touch the row — which is how it learns to
discard its result. The queue verb would also refuse, so the guard is
belt-and-braces by construction rather than by coordination.

### The rung the run was admitted under

`ReviewRequest.mode` is the ladder rung the run was admitted under, so an
engine can spend less rather than the governor's only lever being refusal.
`Claim` does not carry it, and must not: `queue.py` imports nothing from the
governor, which is what keeps the budget out of the claim signature.

`Governor` therefore gains one small read:

```python
def admitted_mode(self, claim: Claim) -> Mode | None:
    """The rung ``claim``'s unsettled reservation was admitted under."""
```

One `SELECT mode FROM ledger WHERE dedupe_key = ? AND owner = ? AND
settled_at IS NULL`. It reads back what `admit` already wrote, rather than
adding a return channel through `claim`'s `admit` hook, whose signature is a
plain `bool` predicate precisely so no budget type appears in the queue's
API. `None` means the reservation is gone — the lapsed-lease case again — so
the run is discarded before it starts, touching neither the row nor the
ledger.

## The loops

Two loops in one process, on one event loop.

```text
poll loop      : poll → classify → enqueue → wait(adaptive 10–600 s)
worker loop ×N : claim → run → repeat;  nothing to claim → wait(30 s)
```

`WORKER_IDLE = 30.0` seconds, waited on the same stop event the poll loop
uses, so `SIGTERM` is not held for the remainder of an idle wait.

Thirty seconds rather than five is about noise, not latency. When the
governor refuses — the 85 % rung, or a contributor over their share — `claim`
returns `None` and the row stays `pending`, indistinguishable to the worker
from an empty queue. It will keep refusing until the window rolls, which can
be hours, and `Governor._allows` logs a warning on every refusal. Five
seconds would mean some 720 identical warnings an hour in the operator's
journal.

### The supervisor

An unexpected exception must not stop all reviews, and must not spin
silently. `daemon.supervise(worker, stop)` awaits `run_forever`, logs the
traceback, and re-enters the loop after a delay that doubles from
`RESPAWN_BACKOFF = 5.0` s to `RESPAWN_BACKOFF_MAX = 300.0` s, reset to the
floor whenever `worker.completed` advanced since the last spawn. Both are
module constants: a backoff is a property of the failure mode, not an
operator's decision, and a configurable one is a knob nobody can set
correctly.

Because the worker holds no state between runs, re-entering the loop *is* a
fresh worker; no object is rebuilt.

A poison pull request cannot tight-loop this. An uncaught crash never
releases the row, so it stays `claimed` under a live 30-minute lease: the
respawned worker takes other work, and when the lease lapses the row is
retried with `attempts` incremented, reaching `abandoned` after three. The
crashed run's reservation stays charged, which is `BUDGET.md`'s lost-worker
rule unchanged. What the backoff is actually for is the crash that is *not*
row-specific — a full disk, a bug in `claim` — where the worker dies holding
nothing.

### Wiring

`daemon.run` sweeps the workspace once at startup (the only safe moment to
remove a stale git lock file), then:

```python
await asyncio.gather(
    daemon.run_forever(stop),
    *[supervise(w, stop) for w in workers],
)
```

Workers are built from `config.worker.count`, each with a distinct owner id.
The engine is `FakeEngine`, and startup logs a warning naming it: a daemon
that looks like it reviews and does not is worse than one that says so.

## Why not a process per review

Considered and rejected. The overhead argument is sound — spawning is
negligible against a multi-minute review — but three other things are not:

1. **It does not remove the loop.** Something must still notice a claimable
   row. An enqueue event is not sufficient: a released row and a
   budget-refused row produce no enqueue. The supervisor would poll the queue
   on an interval, so the interval question survives unchanged.
2. **It breaks the workspace's stated invariant.** `repo.py` serialises every
   write to the mirror's ref namespace with one `asyncio.Lock`, sufficient
   "because the daemon is a single process". Two processes fetching into one
   bare mirror contend on `packed-refs.lock`, and `sweep()`'s reasoning that
   at startup "no git of ours is running" stops being true. Making that safe
   is a redesign of a subsystem that has just landed. The SQLite side, by
   contrast, would be fine: WAL, `busy_timeout`, `BEGIN IMMEDIATE`.
3. **It makes settle-on-failure worse.** A dead child leaves an exit code and
   no usage, so the row stays `claimed` with an unsettled full reservation
   until the lease expires — exactly the lost-worker path a caught failure is
   meant to avoid. In-process, a `finally` settles properly.

Containment does not pay for it either: the engine adapter is already a
subprocess with its own working directory, a scrubbed environment and a
kill-on-timeout. A Python process around it duplicates a boundary that
exists.

What *is* available is concurrency across pull requests in one process, which
is what `worker.count` is.

## Config

```yaml
worker:
  count: 1
```

`WorkerConfig(count: int = 1)`, validated as an integer in 1–4.

The cap is a spending bound, not fussiness: every concurrent run reserves
`max_run_tokens` up front, so `count` multiplies the floor below which the
governor refuses everything. `CLAUDE.md` §5 requires a change that widens
what can spend to say so and to pin the new bound, so tests pin both the
default of 1 and the refusal of 5.

One pull request is never reviewed by two workers whatever `count` is — the
per-pull-request lease enforces that — so `count` parallelises across pull
requests only.

## Tests

`tests/test_worker.py`, over a `tmp_path` store, the existing loopback-git
double and a fake `GitHubClient`. No network, no tokens.

- Every claim passes `admit=governor.admit`: a spy queue asserts the hook it
  was handed, so "nothing spends outside the governor" is pinned by a test
  rather than held by review.
- Settle on success records `result.usage`; on an engine failure records the
  full reservation; on a pre-engine failure records 0 / `unavailable`.
- `PullRequestTooLarge` and `PayloadError` abandon; client, workspace and
  engine failures release with `attempts` intact.
- A refused claim leaves the row `pending` with its attempt count unchanged.
- A worker whose lease was re-claimed settles nothing and finishes nothing.
- End to end: enqueue → claim → fake review → settle → one ledger row
  carrying engine, model, mode, tokens and `usage_confidence`.
- Two successive runs on one worker, asserting the second sees nothing
  carried from the first.

`tests/test_queue.py` gains `abandon`: it sets `abandoned`, is owner-guarded,
and refuses a row whose lease has moved on.

`tests/test_config.py` pins `worker.count`'s default, its bounds and its
absence.

`tests/test_daemon.py` gains the supervisor's backoff and its reset, and a
test that a poll cycle completes while a slow review is in flight — the
issue's "the poll cycle is not blocked" criterion, asserted rather than
argued.

## Documentation

New `docs/WORKER.md`. Edits where this change makes existing text false:
`QUEUE.md` (the new verb, and the status table), `BUDGET.md`
(settle-on-failure), `ENGINE.md`, `ARCHITECTURE.md` and `ROADMAP.md` (the
seam now has a caller), `DAEMON.md` (two loops), `CONFIG.md` (the `worker`
section).
