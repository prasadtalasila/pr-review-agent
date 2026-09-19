# The daemon loop

What runs continuously, and what it is careful not to do. Implemented in
`src/pr_review_agent/daemon.py`.

The loop is the wiring between two halves that already existed. The
[poller](POLLER.md) knows what changed, the [classifier](TRIGGERS.md) knows
what may be reviewed, and the [queue](QUEUE.md) knows what has already been
paid for.

## 🔁 One cycle

```text
cycle = await poller.poll_once()
changed = cycle.changed_items()          # a 304 endpoint is absent entirely

OPEN_PULLS      → payloads.pull_requests → classify_pull_request → enqueue
ISSUE_COMMENTS  ┐
REVIEW_COMMENTS ┘→ payloads.comments     → classify_comment      → enqueue

advance_watermark("pull_requests", newest created_at seen)
advance_watermark("comments",      newest updated_at seen)
```

Then wait — for the [adaptive interval](POLLER.md), or until a signal arrives,
whichever is first.

## 🛑 Where the poll cycle stops

**At `enqueue`.** The cycle claims nothing, calls no review engine and posts
nothing, so it spends no tokens. A claim is the single point at which work
becomes expensive, and that point belongs to the [budget
governor](BUDGET.md) — which is why this could be built before the governor
existed without crossing the one rule in [DESIGN.md](DESIGN.md#-the-one-rule).

What drains the queue is the [review worker](WORKER.md), which runs as its
own task in the same process and claims through the governor. Nothing about
the cycle above changed when it landed: polling still stops at `enqueue`.

## 🔁 Two loops, one process

The poll cycle and a review have different cadences — an adaptive 10–600 s
against minutes — and different failure modes, so they are separate tasks
gathered by `run()`:

```text
asyncio.gather(
    daemon.run_forever(stop),                 # poll → classify → enqueue
    *[supervise(worker, stop) for worker in workers],   # claim → review → settle
)
```

Both wait on the same stop event. A review therefore never holds up a poll,
and a `SIGTERM` reaches both.

`build_workers` makes `worker.count` of them (default 1, capped at 4), each
with a distinct owner id, all sharing the one queue and the one governor.
`supervise` restarts a worker that falls over, with a capped exponential
backoff; see [WORKER.md](WORKER.md#-the-supervisor).

**The engine is the one `config.engine` names**, built by `build_engine` and
shared by every worker. Startup logs a warning naming it, its model and the
budget state, because that line is where the agent starts costing money.
`FakeEngine` is a test double and never reaches a running daemon.

## 🥶 Cold start is the spend bound

A watermark that has never been set is seeded to the moment the daemon
started, and `seed_watermarks` does that for both names at startup — before
the first poll, so the bound is process start rather than
first-successful-poll, which would drift later every time an early poll
failed.

**Nothing that pre-dates the daemon's first start is ever enqueued.** Without
it, a fresh database re-offers the whole open backlog as new pull requests and
replays every historical `@claude` as a new request. That is the failure
[STORAGE.md](STORAGE.md#-watermarks) describes, and on a repository with a
long-lived backlog it would spend the weekly allowance in a single pass.

A restart against an existing database resumes exactly, because
`advance_watermark` only ever moves forward. A daemon that was down for a day
correctly picks up that day's work when it returns.

## ⏱ Two rules about the watermark

**It advances to the newest timestamp seen in the payload, never to `now`.**
An item that exists but is not yet visible to the API would otherwise fall
into the gap between the newest item seen and wall-clock now, and be skipped
forever. A max-seen mark cannot skip anything.

**It advances only after the enqueue.** A crash in between costs one
re-classification, which the dedupe key makes free. The reverse order loses
the trigger permanently.

## 💬 One watermark, two comment endpoints

[STORAGE.md](STORAGE.md#-watermarks) names exactly two watermarks, and both
comment endpoints feed `comments`. GitHub's `updated` only ever moves forward,
so a single high-water mark cannot hide a comment that surfaces later on the
other endpoint.

Editing `@claude` into an old comment therefore *does* summon a review: the
edit bumps `updated_at`, so the comment becomes fresh. That is the right
reading of an allowlisted maintainer's intent. A comment that was already a
mention is stopped from being reviewed twice by its
[dedupe key](TRIGGERS.md#-dedupe-keys), not by the watermark.

## 📝 Known consequences

Both pre-date this loop and are unchanged by it; they are written down here
because this is where an operator will notice them.

- **A draft pull request is not auto-reviewed even after it is marked ready.**
  It is rejected `draft`, the watermark advances past it, and `created_at`
  never changes — so it stays below the mark. The designed escape hatch is an
  allowlisted `@claude`, which `classify_comment` deliberately permits on a
  draft.
- **A pull request opened in the same instant as a cold start may be missed.**
  Seeding compares with `<=`, so the race window is the milliseconds between
  the seed and the first poll, and only on a fresh database.

## 🚨 Errors and shutdown

A `GitHubClientError` is logged at `ERROR` and the cycle skipped: a transient
network failure must not kill a daemon. Anything else propagates. A daemon
that keeps polling while failing to enqueue looks healthy and reviews
nothing, so an unexpected bug should crash loudly rather than spin silently.

A worker is treated differently: an unexpected exception there is logged and
the loop restarted, because killing the process for a fault confined to one
worker would stop the poller too. See
[WORKER.md](WORKER.md#-the-supervisor).

`SIGINT` and `SIGTERM` set an `asyncio.Event`, and the wait between cycles is
on that event rather than a plain sleep — otherwise a `SIGTERM` arriving early
in a 600 s idle interval would hang a service restart for the remainder of it.

## ▶️ Running it

```bash
GITHUB_TOKEN=... poetry run python -m pr_review_agent.daemon
```

`--config` points at a config file other than `./config.yaml`. The token is
read from the environment, never from `config.yaml`, and is never printed.
Exit status is `2` when the token or the config file is missing, and `0` on a
clean shutdown.

The SQLite file comes from [`store.path`](CONFIG.md), default `state.db`. A
relative path is resolved against the working directory the daemon starts in,
so the resolved absolute path is logged at `INFO` on startup — pointing at the
wrong file costs the queue's memory of what has already been reviewed.

Run the [bootstrap checks](https://github.com/prasadtalasila/pr-review-agent/blob/main/DEVELOPER.md#-bootstrap-checks) first on a host
that has never run the daemon.
