# Architecture

What runs, what each part is responsible for, and how much of it exists
today. For *why* the shape is this one, see [DESIGN.md](DESIGN.md).

## 🏗 How it fits together

A single Python asyncio daemon with a SQLite (WAL) store, running on a
private host, runs **two loops in one process**: the poll loop and the
review worker loop. The client and poller are `asyncio`-native rather than
synchronous, and that is not speculative — the queue, the per-pull-request
leases and the budget governor are all built on top of the client, and a
blocking `get` would stall the whole loop for the duration of a rate-limit
backoff. Both loops are gathered by `run()` and wait on the same stop event,
so a `SIGTERM` reaches both and a slow review never holds up a poll:

```text
asyncio.gather(
    daemon.run_forever(stop),                         # poll → classify → enqueue
    *[supervise(worker, stop) for worker in workers],  # claim → review → settle
)
```

**The poll loop** learns what changed, decides whether it may be reviewed,
and stops at `enqueue`: it claims nothing, calls no review engine and posts
nothing, so it spends no tokens. A claim is the single point at which work
becomes expensive, and that point belongs to the **review worker loop**,
which claims through the budget governor, checks the pull request out, runs
the configured engine, settles the ledger and closes the row. `build_workers`
makes `worker.count` of them (default 1, capped at 4), each with a distinct
owner id, all sharing the one queue and the one governor; see
[WORKER.md](WORKER.md) and [DAEMON.md](DAEMON.md) for the ordering rules and
the cold-start bound that keeps a fresh database from paying for the
backlog.

**The ordering of the pieces below is deliberate rather than convenient: the
budget governor lands before the review worker**, so the spending rails
exist before anything can spend. Component 8, the engine adapter, is the
only one that is agent-specific, and it is a **process boundary, not a
library call**: every adapter is a command-line tool (`claude`, `codex`,
`opencode`) run as a subprocess, and no vendor SDK is linked. That is what
makes the boundary a containment boundary — its own working directory, its
own environment, a kill-on-timeout — over a tree the agent treats as
untrusted. [DESIGN.md](DESIGN.md#-generalisation-to-other-agents) has the
reasoning and the cost.

**Dependencies between modules point one way only**, and that direction is
what keeps the trigger suite free of HTTP — it is pure functions over
fixtures, needing no network and spending no tokens:

- `config` builds a `Classifier`; it knows nothing about HTTP.
- `poller/payloads.py` imports `triggers/models.py`, never the reverse.
- Nothing in `triggers/` imports `poller/`.
- `poller/pulls.py` imports `workspace`, never the reverse. `workspace/` is
  pure git and filesystem, so its suite runs with no HTTP at all.
- `engine/` imports `workspace`, `triggers` and `budget`. Only `worker.py`
  imports `engine/`: it is the leaf the whole design is arranged around, and
  the worker is its single caller. See [ENGINE.md](ENGINE.md).
- `worker.py` is where `queue`, `budget`, `workspace`, `engine` and
  `poller/pulls.py` meet, and nothing imports it but `daemon.py`.

The same rule is why `PullRequestFacts` is defined in `workspace/` and mapped
in `poller/`: the module that issues the request depends on the module that
consumes it, not the other way round. And it is why `budget.py` imports
`queue.py` and never the reverse — the budget seam is an optional `admit`
predicate on `claim()`, so the queue stays unaware of the governor, and the
reservation is taken inside the *same* transaction as the queue claim; that
invariant is the one the whole storage choice rests on, spelled out in
[BUDGET.md](BUDGET.md#-reserve-then-settle).

`poller/payloads.py` is the seam where every quirk of the GitHub REST shape
is resolved — the three that shaped it are documented in
[POLLER.md](POLLER.md#-mapping-a-payload-to-a-pull-request). The module tree
that these rules describe, and the one Python 3.10 compatibility shim it
costs, are catalogued in
[DEVELOPER.md](https://github.com/prasadtalasila/pr-review-agent/blob/main/DEVELOPER.md#-package-layout).

## 🔁 The path an event takes

```text
GitHub REST ──► Poller ──► payload mapping ──► Classifier ──► Trigger
  (3 endpoints)  (ETag,      (raw dict to        (allowlist,     │
                  interval)   dataclass)          mention,       │
                                                  watermark)     ▼
                                                            Queue + lease
                                                                 │
                                                    reserve ◄────┤
                                                    (governor)   ▼
                                                            Review engine
                                                                 │
                                                    settle  ◄────┤
                                                                 ▼
                                                             Publisher
```

Every box in that diagram exists and is wired end to end: the
[review worker](WORKER.md) joins the governor, the workspace and the engine
seam into one claim-run-settle loop, running as its own task beside the poll
loop, and the [publisher](PUBLISHER.md) turns a settled run into a posted
comment.

What it drives is the configured `claude` CLI adapter, so the path **costs
real allowance** from the claim onwards. The spending rails were finished
first, which is the point of the build order: the adapter arrived into a
system that could already refuse it, and every refusal it meets — the
windows, the ladder, the size gate, the exclusions, the pre-flight estimate —
was in place before anything could spend.

## 🧩 The components

| # | Component | Responsibility | State |
| :-- | :-- | :-- | :-- |
| 0 | **Daemon loop** | Poll on the adaptive interval, classify what changed, enqueue what is accepted, advance the watermarks, and supervise the workers that drain the queue. | implemented — [DAEMON.md](DAEMON.md) |
| 1 | **Poller** | Outbound-only conditional GETs against three repo-wide GitHub REST endpoints. | implemented — [POLLER.md](POLLER.md) |
| 2 | **Classifier + allowlist** | Turn a polled payload into an accepted trigger or a reason code. | implemented — [TRIGGERS.md](TRIGGERS.md) |
| 3 | **Store** | The watermarks, ETags and queue rows that must survive a restart, and the schema migrations that get them there. | implemented — [STORAGE.md](STORAGE.md) |
| 4 | **Queue and lease** | Atomic conditional claim (SQLite has no `SKIP LOCKED`) and a per-PR lease so reviews of one pull request never overlap. The `head_sha` re-check before posting belongs to the publisher, where the live head can be read. | implemented — [QUEUE.md](QUEUE.md) |
| 5 | **Budget governor** | Windows, ladder, reserve-then-settle and the circuit breaker over one ledger. | implemented — [BUDGET.md](BUDGET.md) |
| 6 | **Workspace** | Fetch a pull request head into a bare mirror, check it out into an isolated worktree, diff it against the merge base, tear it down. Executes nothing from the tree. | implemented — [WORKSPACE.md](WORKSPACE.md) |
| 7 | **Review worker** | Claim through the governor, resolve the pull request, check it out, run an engine, settle the ledger, close the row. Its own task, so a review never blocks a poll. | implemented — [WORKER.md](WORKER.md) |
| 8 | **Engine adapter** | A `ReviewEngine` protocol and `Capabilities` record, with CLI-subprocess implementations (`claude`, then one other). No vendor SDK is linked. | implemented — [ENGINE.md](ENGINE.md); a second adapter is deliberately last |
| 9 | **Publisher** | An immediate 👀 reaction, a live `head_sha` re-check, and one comment per pull request — edited in place on re-review. It can post no other kind of write. | implemented — [PUBLISHER.md](PUBLISHER.md) |
| 10 | **Retention sweep** | Purge review content once a pull request merges; keep the ledger. | not started |
