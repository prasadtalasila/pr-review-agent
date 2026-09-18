# The review worker

What drains the queue. Implemented in `src/pr_review_agent/worker.py`, run
and kept alive by the [daemon](DAEMON.md).

The [poll loop](DAEMON.md) stops at `enqueue`. This is the other half: it
claims a trigger through the [budget governor](BUDGET.md), resolves the pull
request, puts it [on disk](WORKSPACE.md), hands it to a [review
engine](ENGINE.md), records what it cost, and closes the row.

**It does not publish.** Findings are logged and dropped, because the
publisher does not exist yet.

**It does spend.** The engine it runs is the configured `claude` CLI adapter,
so a claim here is real money. Everything between the claim and the
subprocess is what bounds that: the governor's windows and ladder at
admission, the size gate and path exclusions at checkout, the pre-flight
estimate immediately before the engine, and `worker.count` over the whole
lot. `FakeEngine` is a test double and is never wired into a running
daemon.

## 🔁 One run

```text
claim(admit=governor.admit)        the lease and the reservation, one commit
  → GET /pulls/{n}                 head_sha for a mention, and the size counts
  → workspace.checkout(...)        the tree, the merge-base diff, exclusions
  → governor.preflight(...)        the last free refusal -- releases its own hold
  → engine.review(request)         the only agent-specific step, and the spend
  → governor.settle(claim, usage)  release whatever was not spent
  → complete / release / abandon   close the row
```

### The pre-flight estimate

`governor.preflight(claim, checkout.reviewed.lines, now)` is the last point
at which a run can be refused for free: the tree is on disk but no engine has
started, so nothing has been spent. It refuses a pull request predicted to
cost more than `max_run_tokens`, and one with nothing left to review once
`budget.excluded_paths` has been applied.

**It releases the reservation inside that call**, so the worker must not
settle afterwards — a second settle on a settled row is the one mistake this
arrangement is designed to make impossible. The row is then abandoned: both
refusals are deterministic for this head, and another attempt would reserve
allowance only to reach the same answer.

`checkout.reviewed` rather than `facts` is what it is asked about. Those are
different numbers on purpose: the API's counts cover every changed path,
while `reviewed` is what survived the exclusions, which is both what the gate
measures and what the engine is shown.

`settle` runs **before** the queue verb, and both are guarded on the owner.
A worker whose lease lapsed mid-run gets `False` from `settle` and stops
there — which is how it learns to discard a result it is no longer entitled
to act on.

### The two loops, on one timeline

The interleaving is the part that is hard to see from the code, because
`await` is what makes it happen and nothing names it. Both loops run in one
process on one event loop.

| Time | Poll loop | Worker loop |
| :-- | :-- | :-- |
| t+0 s | `GET /pulls` → 200, PR #42 is new. The classifier accepts it, `enqueue` inserts a `pending` row, the watermark advances. | idle |
| t+2 s | sleeping | `claim()` opens one transaction: `governor.admit` measures the windows, writes a reservation for `max_run_tokens`, and the lease is taken in the same commit. |
| t+3 s | polls again, enqueues an `@claude` on PR #7 | `GET /pulls/42` → `PullRequestFacts` |
| t+5 s | waiting | fetch, `merge-base`, diff, `worktree add` |
| t+5 s … t+4 min | **keeps polling** | `await engine.review(...)` — minutes, for a real adapter |
| t+4 min | | teardown, `settle` at what the engine reported, `complete` |
| t+4 min | | claims PR #7's mention |

The mention on #7 waited four minutes. That is what a concurrency of one
means, and it is what `worker.count` exists to change.

## 💸 What a failed run settles at

`settle` runs on **every** path, including a failure. A reservation that is
never settled stays charged at its full ceiling until it ages out of its
rolling window — the deliberate behaviour for a *lost* worker, whose process
died. A caught exception is not a lost worker.

But "settle at what was actually spent" is unanswerable for a crashed
engine: a CLI adapter killed on a timeout has spent real tokens and reports
none. So the rule splits on the one thing that is knowable — whether the
engine had started.

| Where it failed | Settles at | Why |
| :-- | :-- | :-- |
| before `engine.review` | `0`, `unavailable` | Nothing reached an engine. Provable, not assumed. |
| in or after `engine.review` | the full reservation | Anything may have been spent, and the governor cannot find out. |
| nowhere — it succeeded | `result.usage` | What the engine reported. |

In the code this is one variable taking three values, and the assignment
that raises it to the full reservation sits on the line *before* the engine
call. "Did the engine start" is expressed by control flow rather than by a
flag that could disagree with reality.

Pessimism is the safe direction for a spending control. It is the same
argument [BUDGET.md](BUDGET.md#a-crashed-workers-reservation-stays-charged)
already makes for refusing a crashed worker an amnesty.

## 🎫 What happens to the row

A failed run has two possible fates and they are not interchangeable.

| Failure | Fate | Why |
| :-- | :-- | :-- |
| `Outcome.TRUNCATED` | `release` | The run was cut off with work outstanding; another attempt may finish it |
| `Outcome.FAILED` | `abandon` | Anything else that went wrong inside a run that still answered |
| pre-flight refusal | `abandon` | Deterministic for this head, and already settled at zero |
| `GitHubClientError` | `release` | 5xx, rate limit, network — transient by construction |
| `WorkspaceError`, `GitCommandError` | `release` | A failed fetch is a failed network call |
| the engine raises anything | `release` | May succeed next time; bounded by `max_attempts` |
| `PullRequestTooLarge` | `abandon` | Deterministic on this head |
| `PayloadError` | `abandon` | Deterministic |

`release` returns the row to `pending` with its attempt already counted, so
three failures reach `abandoned`. `abandon` gives up at once.

The first two rows are not exceptions: a run can end badly and still return a
`ReviewResult`, because it spent tokens and the ledger has to hear about
that. `Outcome` decides what becomes of the row; it never decides whether the
run settles. Only `COMPLETED` counts as progress for the supervisor's
backoff.

**Why not `complete` for a permanent failure?** Because `done` means
*reviewed*. A pull request refused for its size was never reviewed, and an
operator reading the table should not have to guess which kind of `done`
they are looking at. That is the whole reason
[`ReviewQueue.abandon`](QUEUE.md#-statuses) exists as a verb rather than
being reachable only by exhausting `max_attempts`.

**Why not retry a permanent failure anyway?** Two more attempts reach the
same refusal, and each one reserves allowance to get there.

### `PullRequestTooLarge` is caught first

It subclasses `WorkspaceError`, so the order of the `except` clauses is what
makes it abandon rather than retry. A test pins that.

### Any engine failure is an engine failure

Every adapter is a foreign command-line tool. The worker has no catalogue of
what one can raise and no way to tell a timeout from a parse error from a
bug inside it — so the engine call is wrapped, and anything it raises
becomes `EngineError` and is retried.

Narrowing that boundary to the one call is the point: a bug in the *worker*
is not caught by it, and propagates to the supervisor instead of being
quietly retried three times.

## 🧹 What a run leaves behind

The tree under review is untrusted input, so "one pull request cannot affect
the next" is a security property, not tidiness.

| State | Lifetime | Reaches the next run? |
| :-- | :-- | :-- |
| worktree at `runs/<uuid>` | removed in `finally` | No — and `sweep()` clears the whole directory at startup |
| run ref `refs/run/<uuid>` | deleted in `finally` | No |
| the bare mirror | persistent, shared | **Yes** — the one shared artefact |
| the `ReviewWorker` instance | process lifetime | No field holds run data |
| the engine | a subprocess per run: own `cwd`, scrubbed environment, kill-on-timeout | No |

So the entire carry-over surface is the mirror, and what a hostile pull
request can put there is already bounded by
[WORKSPACE.md](WORKSPACE.md): objects and one namespaced ref, fetched over
an https-only whitelist, never executed, with the diff computed in the bare
repository so in-tree `.gitattributes` cannot hide the change. A large pull
request can grow the disk. It cannot reach the next review's tree.

The worker holding no state is the last link in that chain, which is why it
is a rule rather than an implementation detail. The one field that does
persist, `completed`, counts finished runs and carries nothing from them.

## 🩺 The supervisor

`daemon.supervise` awaits `worker.run_forever` and, on an unexpected
exception, logs the traceback and re-enters the loop. Because a worker holds
no state between runs, re-entering the loop *is* a fresh worker.

The delay doubles from `RESPAWN_BACKOFF` (5 s) to `RESPAWN_BACKOFF_MAX`
(300 s), returning to the floor whenever the worker finished a review in
between: progress means the fault was not persistent, so the accumulated
delay is not earned. Both are module constants, not configuration — a
backoff describes a failure mode rather than an operator's preference.

**Why not let the process die**, as the poll loop does for an unexpected
error? Because that takes the poller down for a fault confined to the
worker, and a daemon that polls but never drains is the state this whole
change exists to end.

**A poison pull request cannot drive the respawn loop.** An uncaught crash
never releases the row, so it stays `claimed` under a live 30-minute lease:
the restarted worker takes other work, and when the lease lapses the row is
retried with its attempt counted, reaching `abandoned` after three. The
backoff is for the crash that is *not* about any row — a full disk, a bug in
the claim path — where the worker dies holding nothing.

## ⏱ Waiting

When `claim()` returns nothing, the worker waits `WORKER_IDLE` (30 s) on the
same stop event the poll loop uses, so a `SIGTERM` is never held for the
remainder of an idle wait.

Thirty seconds rather than five is about noise, not load — a claim is a
local SQLite read. When the governor is refusing (the 85 % rung, or a
contributor over their share) the claim returns `None` and the row stays
`pending`, indistinguishable here from an empty queue, and the governor logs
a warning every time. It will keep refusing until the window rolls, which
can be hours; at five seconds that is some 720 identical lines an hour in
the operator's journal.

## 🔢 `worker.count`

```yaml
worker:
  count: 1
```

A spending control, not a throughput knob. Every concurrent run reserves
`budget.max_run_tokens` **up front**, so `count` multiplies the floor below
which the governor refuses everything. It is capped at 4, and both the
default and the cap are pinned by tests — `CLAUDE.md` §5.

One pull request is never reviewed by two workers whatever this is: the
[per-pull-request lease](QUEUE.md#-one-pull-request-one-worker) holds that.
Raising it parallelises *across* pull requests only.

Each worker gets a distinct owner id, and that is load-bearing rather than
cosmetic: `complete`, `release`, `abandon` and `settle` are all guarded on
the owner, so two workers sharing one could finish each other's rows.

## 🚫 Why not a process per review

The idea is reasonable — process spawn is negligible against a multi-minute
review — and it was rejected for three reasons that are not about overhead.

1. **It does not remove the loop.** Something must still notice a claimable
   row, and an enqueue event is not enough: a released row and a
   budget-refused row produce no enqueue. A supervisor would poll the queue
   on an interval, so the pacing question survives unchanged.
2. **It breaks the workspace's stated invariant.** One `asyncio.Lock`
   serialises every write to the mirror's ref namespace, and that is
   sufficient [because the daemon is a single
   process](WORKSPACE.md#-one-mirror-a-worktree-per-run). Two processes
   fetching into one mirror contend on `packed-refs.lock`, and `sweep()`'s
   reasoning that at startup "no git of ours is running" stops being true.
   The SQLite side would be fine — WAL, `busy_timeout`, `BEGIN IMMEDIATE` —
   but the git side is a redesign.
3. **It makes settle-on-failure worse.** A dead child leaves an exit code
   and no usage, so the row stays `claimed` with an unsettled full
   reservation until the lease expires: exactly the lost-worker path a
   caught failure exists to avoid.

Containment does not pay for it either — the engine adapter is already a
subprocess with its own working directory, a scrubbed environment and a
kill-on-timeout. A Python process around it duplicates a boundary that
exists.

## 🚧 What lands next

The budget pieces that still need a running engine: the circuit breaker,
layer 3's per-run enforcement and the ladder's 60 % rung. Then the publisher,
which turns `ReviewResult.findings` from something logged into something
posted, and which owns the `head_sha` re-check — until it exists, a review of
a commit that has since been superseded is simply discarded.
