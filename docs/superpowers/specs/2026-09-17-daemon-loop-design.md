# Daemon loop — design

Status: approved 2026-09-17. Implements ROADMAP item 1, in parallel with the
budget governor on a sibling branch.

## 🎯 Goal

Close the gap between the poller and the queue: run `poll_once()` on the
adaptive interval, map each changed payload through the classifier, enqueue
what it accepts, and advance the watermarks — so an accepted trigger becomes a
durable queue row without a human running anything by hand.

The loop **stops at `enqueue`**. It claims nothing, calls no review engine and
spends no tokens. That is what makes it safe to build before the budget
governor exists, and it is the fence that keeps this branch clear of the
governor's `claim()` rewrite.

## 📐 Scope

In:

- `src/pr_review_agent/daemon.py` — `Daemon.run_once()`, `run_forever()`,
  `main(argv)`.
- A `store:` section in `config.yaml`.
- Closing the comments-watermark gap: `Comment.updated_at`, mapped in
  `payloads.comments`, checked in `Classifier._decide_comment`.
- `docs/DAEMON.md`, plus a Celery row in the `DESIGN.md` alternatives table.

Out:

- Claiming, leasing, review engines, publishing — the governor and worker
  phases own those.
- `SIGHUP` config reload. `BUDGET.md` requires `budget.enabled` and
  `publish.dry_run` to take effect without a restart; neither key exists yet,
  so the reload interface is designed when the keys that need it are written.

## 🧩 The cycle

```text
cycle = await poller.poll_once()
changed = cycle.changed_items()          # a 304 endpoint is absent entirely

OPEN_PULLS      → payloads.pull_requests → classify_pull_request → enqueue
ISSUE_COMMENTS  ┐
REVIEW_COMMENTS ┘→ payloads.comments     → classify_comment      → enqueue

advance_watermark("pull_requests", newest created_at seen)
advance_watermark("comments",      newest updated_at seen)
```

Two ordering rules carry the correctness.

**Enqueue before advancing the watermark.** A crash between the two makes the
next cycle re-classify, and `INSERT OR IGNORE` on the dedupe key makes that
free. The reverse order loses the trigger permanently.

**Advance to the newest timestamp seen in the payload, never to `now`.** An
item that exists but is not yet visible to the API would otherwise fall into
the gap between the newest seen item and wall-clock now, and be skipped
forever. A max-seen mark cannot skip anything.

### One watermark for two comment endpoints

`STORAGE.md` names exactly two watermarks, and both comment endpoints feed
`comments`. That is safe because GitHub's `updated` only moves forward: a
comment that surfaces later always carries a later timestamp than the mark, so
a shared high-water mark cannot hide it.

The visible consequence is that **editing `@claude` into an old comment
triggers a review**, because the edit bumps `updated`. An allowlisted
maintainer doing that *is* the ask. Where the comment was already a mention,
its dedupe key (`mention:repo:pr:comment_id`) makes the re-classification a
no-op.

## 🥶 Cold start — the spend bound

`_since(name, now)` reads the stored watermark; when there is none it seeds
the mark to `now`, logs at `INFO`, and returns it. `seed_watermarks(now)`
calls that for both names once at startup, so the bound is process start
rather than first-successful-poll.

**Nothing that pre-dates the daemon's first start is ever enqueued.** Without
this, a fresh database re-offers the whole open backlog as new pull requests
and replays every historical `@claude` as a new request — the failure
`STORAGE.md` describes, and the one this branch must not introduce.

Restart against an existing database resumes exactly, because
`advance_watermark` is already monotonic. A daemon down for a day correctly
picks up that day's work when it returns.

Known consequence, pre-existing and unchanged by this branch: a pull request
opened as a draft is rejected `draft`, the watermark advances past it, and
`created_at` never changes — so it is not auto-reviewed even after it is
marked ready. The designed escape hatch is an allowlisted `@claude`, which
`classify_comment` deliberately permits on a draft.

## 🕳 The comments-watermark gap

`Comment` carries no timestamp today and `_decide_comment` has no freshness
check, so the `comments` watermark that `STORAGE.md` specifies cannot be
applied. Closing it:

- `Comment.updated_at: datetime`, required, placed before the defaulted
  `head_sha`;
- `payloads.comments` maps `item["updated_at"]` through the existing
  `parse_timestamp`; a missing key is skipped with a warning like any other
  unmappable item;
- `_decide_comment` returns `Decision(None, "not_fresh")` when
  `updated_at <= since`, ordered after the bot check and before the mention
  scan — a timestamp compare is cheaper than a regex, and `not_fresh` is
  already in `NOISY_REASONS`, so it stays at `DEBUG`.

This only ever **narrows** what triggers a review.

## ⚙️ Configuration

```yaml
store:
  path: state.db        # optional; this is the default
```

`StoreConfig` is a frozen dataclass parsed like the existing sections, and
unknown keys inside `store:` are rejected as everywhere else. The section
itself is optional: a missing path provably cannot spend tokens now that cold
start bounds a fresh database, and keeping it optional avoids editing the
eight shared fixtures in `tests/test_config.py` that the governor's `budget:`
section will also touch.

The daemon logs the **resolved absolute path** at startup, so a relative path
interpreted against an unexpected working directory is visible rather than
silent.

`GITHUB_TOKEN` stays in the environment, never in `config.yaml`, exactly as
`bootstrap.py` reads it.

## 🛑 Errors and shutdown

`GitHubClientError` from a poll is logged at `ERROR` and the cycle skipped: a
transient network failure must not kill a daemon. Bare `Exception` is **not**
caught — an unexpected bug should crash loudly rather than spin silently.

`main` installs `SIGINT`/`SIGTERM` handlers that set an `asyncio.Event`, and
`run_forever` waits on that event with the adaptive interval as its timeout,
so a `SIGTERM` during a 600 s idle interval exits promptly instead of hanging
a service restart. `loop.add_signal_handler` raises `NotImplementedError` on
Windows, which CI spot-checks, so there is a `signal.signal` fallback.

Store and client close in `finally`.

## 🚀 Entry point

`python -m pr_review_agent.daemon --config config.yaml`, mirroring
`bootstrap.main`: exit `2` on a missing token or unreadable config, `0` on a
clean shutdown.

## 🧪 What the tests must pin

A fake poller returning canned `PollCycle`s over a `tmp_path` store. No
network, no tokens.

Spend bounds (`CLAUDE.md` §5):

- a fresh database, given a payload of old allowlisted pull requests and old
  `@claude` mentions, enqueues **nothing**;
- both watermarks are seeded at startup, and a stale value never rewinds one;
- a watermark advances to the newest timestamp seen, not to `now`;
- a failing `enqueue` leaves the watermark unmoved;
- re-polling an identical cycle enqueues once.

Behaviour:

- a 304 endpoint classifies nothing and moves no watermark;
- both comment endpoints share the one `comments` watermark;
- a `GitHubClientError` does not stop the loop;
- a set stop event short-circuits the sleep.

Classifier: `test_comment_older_than_watermark_is_not_fresh`.

## 🔀 Branch boundary

| This branch | Governor branch |
| :-- | :-- |
| `daemon.py`, `triggers/models.py`, `triggers/classifier.py`, `poller/payloads.py` | `budget/`, `store.py` `_MIGRATIONS`, `queue.py` `claim()` |
| `config.py` — adds `StoreConfig` | `config.py` — adds `BudgetConfig` |

Shared files are `config.py`, `config.example.yaml` and the status rows in
`ROADMAP.md` / `ARCHITECTURE.md` / `README.md`. All are append-style
conflicts. No shared function is modified by both.

## 🚫 Rejected: Celery

The reservation must be atomic with the dequeue. Celery dequeues in a broker
while the ledger lives in SQLite, so no transaction spans both — the
distributed-transaction problem `DESIGN.md` already rejects for RabbitMQ.
Recorded in full in the `DESIGN.md` alternatives table.
