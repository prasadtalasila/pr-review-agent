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

### What one run leaves behind

The statelessness rule is worth spelling out against the actual inventory,
because "one pull request cannot affect the next" is a security claim and not
merely a tidiness one. The tree under review is untrusted input.

| State | Lifetime | Reaches the next run? |
| :-- | :-- | :-- |
| Worktree at `runs/<uuid>` | created per run, removed in `finally` | No. `sweep()` also deletes the whole `runs/` tree at startup, so a crash between `worktree add` and teardown is cleaned at the next boot. |
| Run ref `refs/run/<uuid>` | per run, `update-ref -d` in `finally` | No |
| The bare mirror | persistent, shared by every pull request | **Yes** — the one genuinely shared artefact |
| `ReviewWorker` instance | process lifetime | Only if a field held run data. None does. |
| Engine | a subprocess per run: own `cwd`, scrubbed env, kill-on-timeout | No |
| `GitHubClient`, `SqliteStore` | process lifetime, shared with the poll loop | Carries no per-review state |

So the whole carry-over surface is the mirror, and what a hostile pull
request can put there is already bounded by the workspace design: objects and
one ref under `refs/run/`, fetched `--no-tags --no-recurse-submodules` over
an https-only whitelist, never executed. The diff is computed in the *bare*
repository, so an in-tree `.gitattributes` cannot render the change as
"Binary files differ" and hide itself from the review. What a large pull
request can do is grow the disk. What it cannot do is reach the next
review's tree, its environment or its prompt.

This is why the worker holding no state is a design rule rather than an
implementation detail: it is the last link in that chain, and the only one
this change is adding.

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

## Walkthrough: one review, end to end

The interleaving is the part that is hard to see from the code, because
`await` is what makes it happen and nothing names it. Both loops are in one
process on one event loop.

| Time | Poll loop | Worker loop |
| :-- | :-- | :-- |
| t+0 s | `GET /pulls` → 200; PR #42 is new. Classifier accepts (allowlisted author **id**, `created_at` above the watermark). `enqueue` inserts `pull:42:abc123` as `pending`; the watermark advances *after* the insert. Waits the adaptive interval. | idle in `_wait` |
| t+2 s | sleeping | wakes. `claim()` opens `BEGIN IMMEDIATE`: sweeps rows out of attempts, runs `_CLAIMABLE` oldest-first, offers #42 to `governor.admit`, which measures the three shared windows plus the contributor's, finds mode `full`, and inserts a ledger row with `reserved_tokens = max_run_tokens` and `settled_at NULL`. `_TAKE_LEASE` then sets `claimed`, `attempts=1`, `owner`, `leased_until = t+30 min`. **One commit**: the lease and the reservation are atomic, which is the whole concurrency guarantee. |
| t+3 s | wakes, polls, enqueues an `@claude` mention on PR #7 | `await GET /pulls/42` → `PullRequestFacts`. This read resolves `head_sha` (a mention's payload carries none) and supplies the counts the size gate needs — one request serving both. |
| t+5 s | waiting | `workspace.checkout`: size gate → fetch under the mirror lock → `merge-base` → `diff` in the bare mirror → `worktree add --detach`. |
| t+5 s … t+4 min | polls repeatedly, enqueues whatever it sees | `await engine.review(...)`. `FakeEngine` returns at once; a CLI adapter is minutes. **This await is the reason the worker is a separate loop.** |
| t+4 min | | context exit tears down the worktree and the run ref |
| | | `governor.settle` → `used_tokens`, `usage_confidence`, `engine`, `model`, `settled_at`. The windows stop counting the full reservation and start counting what was spent. |
| | | `queue.complete` → `done`, owner cleared. Findings are logged and dropped; there is no publisher. |
| t+4 min | | next `claim()` offers PR #7's mention — a different pull request, so the per-PR lease does not block it |

The mention on #7 waited four minutes behind #42. That is what concurrency
of one means, and it is why `worker.count` exists.

### The same review, failing once

`engine.review` raises `TimeoutError` at t+30 min.

1. `settle` runs anyway, at the **full reservation** — the engine had
   started. That charge stays in the windows until it rolls out.
2. `queue.release` → `pending`, owner cleared, `attempts` still 1.
3. The worker loops and `claim()` offers the same row straight back:
   `attempts=2`, a **fresh** reservation, a fresh 30-minute lease.
4. Attempt 2 succeeds → settle at actual usage → `complete`.

One trigger now has two ledger rows: one charged in full for the failure,
one for the real cost. That is the intended pessimism, not double-counting to
be fixed later — the failed attempt genuinely may have spent what it
reserved, and the governor cannot find out.

Had attempt 3 also failed, the next `claim()` would have swept the row to
`abandoned` before offering anything, and no further allowance would be
reserved for it.

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

`docs/WORKER.md` is a deliverable of this change, not a note appended to it.
The worker is where five subsystems meet, and most of what is worth knowing
about it is *why one ordering was chosen over another* — knowledge that is
invisible in the code and expensive to re-derive. It carries:

1. **What the worker is, and what it deliberately is not.** It drains; it
   does not publish. Findings are logged and dropped until the publisher
   exists.
2. **The two loops**, with the end-to-end walkthrough above — the successful
   interleaving and the failing one — because "the poll cycle is not blocked"
   is a claim best read as a timeline.
3. **The three-valued `usage` and the settle rule**, with the table of where
   a failure happened and what it settles at, and the argument for why a
   crashed engine cannot be settled at zero.
4. **The fate of a failed row**: `release` versus `abandon`, what each costs,
   and why `complete` was not reused for a permanent failure.
5. **What one run leaves behind**, with the carry-over table — the isolation
   argument stated as a property of the code.
6. **The supervisor**: why a crash respawns rather than killing the daemon,
   why a poison pull request cannot tight-loop it, and what the backoff is
   actually for.
7. **Why a process per review was rejected**, in full. This one is worth
   writing down precisely because the idea is reasonable and will be raised
   again; the objections are non-obvious and two of them are invariants
   stated elsewhere in the docs.
8. **`worker.count` as a spending bound**, and the fact that raising it
   parallelises across pull requests but never within one.

Edits where this change makes existing text false: `QUEUE.md` (the new verb,
and the status table, which currently defines `done` as reviewed and
`abandoned` as attempts-exhausted only), `BUDGET.md` (settle-on-failure,
which the module docstring currently discusses only for a lost worker),
`ENGINE.md`, `ARCHITECTURE.md` and `ROADMAP.md` (the seam now has a caller;
the "nothing drains the queue" state is over), `DAEMON.md` (two loops, and
the supervisor), `CONFIG.md` and both example configs (the `worker` section).

Module and class docstrings carry the same reasoning at the point of use, as
`queue.py`, `budget.py` and `repo.py` already do: the settle rule beside
`run_one`, the carry-over rule on the dataclass, the backoff rationale on
`supervise`.

## Decisions, and what was rejected

Recorded because the discarded options are all defensible, and the next
person to look at this will think of them again.

| Decision | Chosen | Rejected, and why |
| :-- | :-- | :-- |
| How much the worker owns | facts read + checkout + engine | *Engine only, checkout injected* — smaller, but the acceptance test would then run over a stub rather than the real workspace, forfeiting the free end-to-end verification that is the point of doing this now. *Reuse the trigger's `head_sha`* — not viable: a mention's is `None`, and the size counts would be unavailable. |
| Settle on failure | split on whether the engine started | *Always zero* — refunds a CLI adapter that timed out after spending real tokens, up to `max_attempts` times. *Always the full reservation* — charges a full run for a pull request refused by the size gate before the first git call. |
| Fate of a failed row | transient `release`, permanent `abandon` | *Everything releases* — reaches the same refusal three times, reserving allowance each time. *Permanent completes* — overloads `done`, which means reviewed, so the table stops distinguishing a reviewed row from a refused one. |
| Worker topology | N loop tasks in one process | *A process per review* — see above; three objections, none of them overhead. *A task per claim behind a semaphore* — equivalent in effect, more moving parts in the shutdown path, and `worker.count` already expresses the same thing. |
| Idle wait | 30 s, fixed | *5 s* — better latency, but a governor refusing for hours logs some 720 identical warnings an hour. *An enqueue event* — fixes latency, not the refusal spin, and adds shared state between the loops; a released or refused row produces no enqueue. |
| Crash handling | supervisor respawns with capped exponential backoff | *Propagate and let the process die*, as the poll loop does — but that takes the poller down for a fault confined to the worker. *Fixed-delay respawn* — noisier under a persistent fault. *A crash budget that stops the daemon* — loudest, same collateral as propagating. |
| Backoff constants | module constants | *Config keys* — a backoff is a property of the failure mode, not an operator's decision, and nobody can set one correctly. |
| `worker.count` | config key, default 1, capped at 4 | *Fixed at 1* — safest, but the loop shape supports N and pinning it in code means a later spending decision is also a refactor. *Fixed at 2* — doubles the reservation floor on the strength of a fake engine that has never spent anything. |
