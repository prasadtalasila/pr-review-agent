# Storage

One SQLite file, in WAL mode, holding everything that has to survive a
restart. Implemented in `src/pr_review_agent/store.py`.

Four things live here: the **watermarks** and **ETags** below, the **queue**
rows whose claim protocol is described in [QUEUE.md](QUEUE.md), and the
**ledger** the [budget governor](BUDGET.md) computes its windows from. The
schema is declared in one place — this module — because migration order has to
be a single sequence.

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

`busy_timeout` is 5 s, so a connection that meets that lock waits rather than
failing on the spot. Several daemons sharing one file can still exceed it —
the reviews are minutes long and the writes are not staggered — and SQLite
reports the lost wait as a plain `OperationalError`. `is_contention` tells
that apart from a genuine fault by its message, because the poll loop skips a
cycle for the first and crashes for the second; see
[DAEMON.md](DAEMON.md#-errors-and-shutdown).

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

Watermarks are namespaced by stream (`pull_requests`, `comments`) because the
two advance independently: comments are sorted by `updated`, pull requests by
`created`. Both comment endpoints share the one `comments` mark — `updated`
only ever moves forward, so a single high-water mark cannot hide a comment that
surfaces later on the other endpoint.

They are namespaced by **repository** as well, so the key is
`pull_requests:owner/name` rather than `pull_requests`. One store is how
several daemons share one token budget — see [BUDGET.md](BUDGET.md) — and an
unqualified key would be written by whichever repository polled last, leaving
every other one reading its own backlog as already seen and skipping it
permanently.

A database written before the key carried a repository holds unqualified
`pull_requests` and `comments` rows. A schema migration cannot rename them: the
repository is named in `config.yaml` and is not in the database at all. So the
daemon adopts them at startup instead, copying each onto its qualified name
before seeding.

Adoption happens **once per store, not once per repository**, and the
unqualified rows are **deleted** in the same transaction. The rows describe the
one repository that was polling before the upgrade, so a second repository
later pointed at that store must not inherit them: it would start from a
timestamp it has never polled — possibly weeks back — and enqueue and pay for
every open pull request since, which is the cold-start spend bound
[DAEMON.md](DAEMON.md) exists to hold. Deleting them costs a downgrade its
watermark, and that is the cheaper of the two.

The [daemon loop](DAEMON.md) is what advances them, to the newest timestamp it
saw in a payload rather than to wall-clock now, and only after the enqueue that
the timestamp accounts for.

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
    name TEXT PRIMARY KEY,  -- "<stream>:<owner>/<name>", e.g. "comments:o/r"
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
    owner        TEXT,
    comment_id     INTEGER,          -- the comment a mention came from
    comment_source TEXT,             -- issue|review: which endpoint it was on
    command      TEXT NOT NULL DEFAULT 'review'  -- review|describe
);
CREATE TABLE ledger (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key       TEXT NOT NULL,   -- the queue row this run is for
    owner            TEXT NOT NULL,   -- the lease holder that reserved it
    actor_id         INTEGER NOT NULL,
    mode             TEXT NOT NULL,   -- the ladder rung it was admitted under
    reserved_tokens  INTEGER NOT NULL,
    used_tokens      INTEGER,         -- NULL until settled
    usage_confidence TEXT,            -- exact|estimated|unavailable
    engine           TEXT,            -- NULL until settled
    model            TEXT,
    reserved_at      TEXT NOT NULL,   -- aware UTC, ISO-8601
    settled_at       TEXT,
    reviewed_lines   INTEGER,         -- what the estimate is fitted against
    stop_reason      TEXT,            -- why the run ended; NULL until settled
    repo             TEXT,            -- NULL before migration 11
    pr_number        INTEGER          -- NULL before migration 11; the pacer reads both
);
-- The one index here that is a constraint rather than a lookup aid: it is
-- what makes "one open reservation per trigger" a rule the schema holds.
CREATE UNIQUE INDEX ledger_open ON ledger (dedupe_key) WHERE settled_at IS NULL;
CREATE TABLE runs (
    dedupe_key        TEXT PRIMARY KEY, -- the queue row and ledger rows it joins
    repo              TEXT NOT NULL,
    pr_number         INTEGER NOT NULL,
    head_sha          TEXT NOT NULL,    -- the commit the review describes
    outcome           TEXT NOT NULL,    -- completed|truncated|failed
    findings          TEXT NOT NULL,    -- JSON array; emptied by the purge
    comment_id        INTEGER,          -- NULL until published
    recorded_at       TEXT NOT NULL,
    published_at      TEXT,             -- NULL until posted (or dry-run)
    content_purged_at TEXT,
    publish_outcome   TEXT,             -- published|superseded|dry_run|refused
    publish_attempts  INTEGER NOT NULL DEFAULT 0,  -- posts tried for this run
    publish_failed_at TEXT,             -- set once it is given up on
    omitted           TEXT NOT NULL DEFAULT '[]', -- what excluded_paths withheld
    assessment        TEXT,             -- JSON; NULL before 18, on a description, once purged
    description       TEXT              -- JSON; NULL on a review, '{}' once purged
);
CREATE TABLE agent_comments (
    repo       TEXT NOT NULL,
    comment_id INTEGER NOT NULL,   -- a comment this agent posted
    posted_at  TEXT NOT NULL,
    PRIMARY KEY (repo, comment_id)
);
CREATE TABLE budget_state (
    key   TEXT PRIMARY KEY,           -- calibrated_pct|tripped_until|last_trip_at
    value TEXT NOT NULL
);
CREATE TABLE budget_policy (
    id             INTEGER PRIMARY KEY CHECK (id = 1),   -- one row, enforced
    authority_repo TEXT NOT NULL,     -- who published it
    policy         TEXT NOT NULL,     -- JSON: the five shared budget fields
    written_at     TEXT NOT NULL
);
```

`budget_policy` is what lets several daemons share one file and therefore one
token budget — see [BUDGET.md](BUDGET.md#-several-repositories-one-allowance).
The `CHECK` holds it to one row because a second row would be a second opinion
about one allowance. It is kept apart from `budget_state` even though both are
small and scalar: that one is what the circuit breaker *learned*, this is what
an operator *declared*, and resetting either must not disturb the other.

`runs` is the **only** table holding review content, and therefore the only
one the retention sweep purges. It is written before the publisher is asked,
which is what lets a failed GitHub write be retried without a second review —
see [PUBLISHER.md](PUBLISHER.md#-a-paid-review-is-kept-until-it-can-be-posted).

`content_purged_at` is stamped separately from emptying `findings`, so a run
whose content was deleted after a merge stays distinguishable from a run that
looked and found nothing.

`queue.comment_id` and `queue.comment_source` are NULL together for a
`pr_opened` row: nobody wrote a comment to acknowledge, so the 👀 goes on the
pull request itself.

`budget_state` holds the [circuit breaker](BUDGET.md#-the-circuit-breaker)'s
three scalars, and is a separate table from `ledger` for a reason worth being
explicit about: ledger rows are tokens genuinely consumed and the rolling
windows **sum** them, so breaker state kept there would be counted as spend.
Key/value because these are three unrelated values rather than a row of one
thing, and every key absent is what "never tripped" reads back as — which is
how a database written before version 9 adopts it with no backfill.

The ledger is append-only and **never** pruned, even when review content is
purged after a merge: the rolling budget windows are computed from it, so
deleting a row would silently hand back allowance that was genuinely spent.
The retention split is *purge content, retain metrics*. See
[DESIGN.md](DESIGN.md#-retention).

**A reservation is identified by its `dedupe_key` while it is open**, and
`ledger_open` is what says so. `Governor.settle` matches on the key *and* the
`owner`, but those two clauses answer different questions: uniqueness — is
there exactly one open reservation for this trigger? — is the index's job,
and entitlement — is it *ours*? — is the owner's, which is how a worker whose
lease lapsed learns to discard a result it may no longer post.

Without the index they were the same clause, and wrongly. `daemon.supervise`
restarts a crashed worker with the same `owner`, so an attempt that died
holding a reservation and then re-claimed the row wrote a *second* open row
with the identical pair. One settle matched both, updated both, reported no
single reservation, and the worker threw away a review it had already paid
for — while the crashed attempt's ceiling was overwritten by the retry's
actual usage, under-counting the spend. `admit` now closes the predecessor as
[`lost`](BUDGET.md#a-lost-reservation-is-closed-by-the-next-admission) inside
the claim's own transaction, so there is never a second open row to be
ambiguous about.

Two columns it deliberately does **not** have. There is no `state`, because a
row is reserved exactly when `settled_at IS NULL`. There is no `expires_at`,
because an unsettled reservation stays charged until it ages out of its
rolling window rather than being released when its lease lapses — see
[BUDGET.md](BUDGET.md#a-crashed-workers-reservation-stays-charged).

`repo` and `pr_number` are absent for the same reason: both dedupe-key
namespaces already carry them, and `queue` rows are kept forever.

`reviewed_lines` is the one column that exists for something other than the
windows: it is the predictor the
[pre-flight estimate](BUDGET.md#-the-pre-flight-token-estimate) fits a
tokens-per-line rate against. It stays NULL on a row that reviewed nothing —
a refusal, or a run recorded before the column existed — so those rows never
enter the fit.

`stop_reason` is why the run ended, which is a different question from
`usage_confidence`'s how far its recorded cost can be trusted. Without it a
run killed on the wall clock and one whose envelope would not parse are the
same row: both `unavailable`, both charged the full reservation, and nothing
to say which control bound the run. One of eleven values — `completed`,
`truncated`, `failed`, `timeout`, `engine_error`, `engine_unavailable`,
`refused`, `closed`, `lost`, `infrastructure`, `usage_limit` — rather than
free text, because it is read by an operator
and, later, by the circuit breaker, and `GROUP BY stop_reason` has to mean
something. Rows written before the column existed keep a NULL: nothing
recorded why they stopped, and inventing a reason would put fiction in the
one table that is never pruned.

Three of those values settle at **zero**, and they are kept apart on purpose:
`refused` is the pre-flight estimate saying no, `engine_unavailable` is a
subprocess that never started, and `closed` is a pull request merged or
closed before the claim reached a checkout. No engine ran in any of the
three, but the operator action differs in each — a limit, a host, or a queue
that is too slow.

## 🔢 Migrations

Schema changes are an ordered list applied on connect, with the file's
`PRAGMA user_version` recording how many have run. Version 1 is the ETag and
watermark tables; version 2 adds the queue; version 3 adds the ledger; version
4 indexes the ledger by `(actor_id, reserved_at)` for the per-contributor
budget window; version 5 adds `ledger.reviewed_lines`; version 6 adds
`ledger.stop_reason`; version 7 adds `queue.comment_id` and
`queue.comment_source`, which is what lets the publisher acknowledge the
comment a mention was written in; version 8 adds `runs`; version 9 adds
`budget_state`; version 10 adds `budget_policy`; version 11 adds
`ledger.repo`, `ledger.pr_number` and the `(repo, pr_number, reserved_at)`
index the [pacer](BUDGET.md#-the-pacer-one-pull-requests-rate) reads; version
12 adds `runs.publish_outcome`; version 13 adds `runs.publish_attempts` and
`runs.publish_failed_at`; version 14 adds `agent_comments`; version 15 adds
the `ledger_open` unique index; version 16 adds `ledger.reviewed_since`;
version 17 adds `runs.omitted`; version 18 adds `runs.assessment`; version
19 adds `queue.command` and `runs.description`, for
[`@claude describe`](DESCRIBE.md).

**Version 15 cleans up before it constrains, and that order is the whole
migration.** A unique index cannot be created over a table that already
violates it, and a store that will not migrate is a daemon that will not
start — so a live database holding duplicate open rows would be bricked by
the index alone. The migration therefore settles the duplicates first: for
every `dedupe_key` with more than one open row it keeps the newest, which is
the one a running worker may still settle, and closes the older ones at their
full `reserved_tokens` with `stop_reason = 'lost'` and `usage_confidence =
'unavailable'`. Their `settled_at` is their own `reserved_at`, because
nothing was learned about those runs after the instant they opened and a
later timestamp would imply otherwise. Both statements are one migration
tuple, so the cleanup and the constraint commit together — a crash between
them cannot leave a database with duplicates and no index.

No allowance is handed back by that cleanup. Those rows were counting their
whole reservation while they sat open and they count exactly the same
afterwards; what changes is that the charge now says why it exists.

Version 13's counter starts at **zero on every row written before it**, which
is the honest reading: nothing counted those posts. A run carrying a stamp is
never offered for publication again, so an operator who has fixed whatever
GitHub was refusing — an unlocked pull request, a restored token scope — puts
the review back in the queue by clearing it:

```sql
UPDATE runs SET publish_failed_at = NULL, publish_attempts = 0
 WHERE dedupe_key = '<key from the ERROR>';
```

The daemon picks it up the next time a publication item names that run; until
then the findings sit in `runs`, which is where they have been all along.

Version 14's `agent_comments` is the agent's memory of what it has said: the
classifier reads it once per poll cycle and drops any comment whose id is
there, which is what stops the agent reviewing its own review
([TRIGGERS.md](TRIGGERS.md#-the-agent-cannot-summon-itself)). It is a table
of its own rather than a column on `runs` because it has to cover every
comment the publisher posts, whether or not a review is behind it, and
because it is **never pruned** — a retention sweep that deletes `runs` rows
must not make the agent forget what it said. It starts empty on an existing
store, which reads the same as a first run: comments posted before the
migration are not known to be the agent's, and `mention.neutralise` is what
covers them.

Version 11's two columns are **NULL on every row written before it**, and
that is the reading the pacer is built around: no repository and no pull
request means no history, which is the same answer a pull request nobody has
reviewed gets. A backfill is impossible — the ledger is keyed by
`dedupe_key`, and the pull request a retired key referred to is not
recoverable from it — so the honest default was the design constraint rather
than an afterthought.

**Each migration and its version bump commit together**, in one transaction.
That is what lets versions 5, 6 and 7 be `ALTER TABLE ADD COLUMN`, which
SQLite has no `IF NOT EXISTS` form for and which fails outright on a second
application.
A crash mid-migration rolls the pair back and the migration is simply
re-applied.

The `CREATE` statements are still `IF NOT EXISTS`, but now for one reason
rather than two: a database created before the list existed already carries
version 1's tables at `user_version = 0`, and has to be able to adopt it.

`executescript` would be the natural way to run a multi-statement migration
and is deliberately not used — it issues a `COMMIT` of its own first, which
would split the script from its version bump.

## 🔐 Write transactions

`SqliteStore.transaction()` runs a block inside one `BEGIN IMMEDIATE`. The
write lock is taken up front rather than on first write, which is what makes a
read-then-write sequence — the queue's [conditional claim](QUEUE.md#-one-pull-request-one-worker)
— atomic against another writer.

That is also where the budget reservation goes, through `claim()`'s `admit`
hook, so that a claim and the allowance it spends commit together or not at
all.
