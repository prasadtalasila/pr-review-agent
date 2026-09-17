# Storage

One SQLite file, in WAL mode, holding everything that has to survive a
restart. Implemented in `src/pr_review_agent/store.py`.

Three things live here: the **watermarks** and **ETags** below, and the
**queue** rows whose claim protocol is described in [QUEUE.md](QUEUE.md).
The schema is declared in one place — this module — because migration order
has to be a single sequence.

## 🗄 Why SQLite

The topology is **one writer on one host**. PostgreSQL's multi-client
concurrency would be capability paid for and never used, while SQLite's write
serialisation actively simplifies the hardest invariant in the system: the
budget reservation has to be atomic with the queue claim, and SQLite's single
write lock gives that for free where Postgres would need explicit row locking.

It also needs no daemon, no port and no DBA on a locked-down host, and
`sqlite3` is in the standard library. Revisit only if the agent ever becomes
multi-host.

WAL mode is on because the later phases (queue, lease, budget governor) read
this file while the poller writes it. The write lock stays single-writer
either way, which is the property the reservation depends on.

## 🧭 Watermarks

The poller sees open pull requests and recent comments, not `opened` events, so
the classifier needs a timestamp below which everything has already been
considered.

Held only in memory, the cost of a restart is not a wasted poll — it is spent
allowance:

- for **pull requests**, the whole open backlog is re-offered as fresh;
- for **comments**, every old `@claude` is replayed as a new request.

A watermark therefore lives on disk, and **only ever moves forward**. A restart
that read a stale row, or two cycles settling out of order, must not walk it
backwards and re-admit events already decided. `advance_watermark` takes the
later of the stored and the offered value and returns whichever is in force.

Watermarks are namespaced by name (`pull_requests`, `comments`) because the two
streams advance independently: comments are sorted by `updated`, pull requests
by `created`.

Values are stored as aware UTC ISO-8601 strings. A naive datetime is rejected
at the boundary for the same reason `Classifier.since` rejects one — a naive
comparison against a GitHub `...Z` timestamp raises `TypeError` at the worst
possible moment.

## 🏷 ETags

Cheaper to lose than a watermark: a missing ETag costs one full `GET` per
endpoint on the next cycle, not correctness. It lives in the same file because
it has the same shape and the same lifetime.

`SqliteStore` satisfies the `ETagCache` protocol, so it drops into `Poller`
exactly where the in-memory `ETagStore` goes:

```python
with SqliteStore("state.db") as store:
    poller = Poller(client=client, endpoints=endpoints, etags=store)
    await poller.poll_once()
```

The in-memory `ETagStore` remains the default. It is what the tests use, and
what a one-shot poll wants.

## 📋 Schema

```sql
CREATE TABLE etags (
    path TEXT PRIMARY KEY,
    etag TEXT NOT NULL
);
CREATE TABLE watermarks (
    name TEXT PRIMARY KEY,
    at   TEXT NOT NULL      -- aware UTC, ISO-8601
);
CREATE TABLE queue (
    dedupe_key   TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    repo         TEXT NOT NULL,
    pr_number    INTEGER NOT NULL,
    head_sha     TEXT,               -- NULL for a mention
    actor_id     INTEGER NOT NULL,
    status       TEXT NOT NULL,      -- pending|claimed|done|abandoned
    attempts     INTEGER NOT NULL DEFAULT 0,
    enqueued_at  TEXT NOT NULL,      -- aware UTC, ISO-8601
    leased_until TEXT,
    owner        TEXT
);
```

The ledger the [budget governor](BUDGET.md) needs is not here yet. It will be
append-only and **never** pruned, because the rolling budget windows are
computed from it — the retention split is *purge content, retain metrics*. See
[DESIGN.md](DESIGN.md#-retention).

## 🔢 Migrations

Schema changes are an ordered list applied on connect, with the file's
`PRAGMA user_version` recording how many have run. Version 1 is the ETag and
watermark tables; version 2 adds the queue.

Every statement is `IF NOT EXISTS`, for two reasons that both come down to
re-runnability. A database created before the list existed already carries
version 1's tables at `user_version = 0`, and has to be able to adopt it. And a
crash between applying a migration and bumping the version must leave the
migration re-runnable rather than the file wedged.

## 🔐 Write transactions

`SqliteStore.transaction()` runs a block inside one `BEGIN IMMEDIATE`. The
write lock is taken up front rather than on first write, which is what makes a
read-then-write sequence — the queue's [conditional claim](QUEUE.md#-one-pull-request-one-worker)
— atomic against another writer.

That is also where the budget reservation is specified to go, so that a claim
and the allowance it spends commit together or not at all.
