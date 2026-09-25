"""The SQLite state that has to survive a restart.

Four things are kept here, and the first three are about *not repeating
work*:

**Watermarks.** The poller sees open pull requests and recent comments, not
``opened`` events, so the classifier needs a timestamp below which everything
has already been considered. Held only in memory, a restart re-offers the
whole open backlog -- and, for comments, replays every old ``@claude`` as a
fresh request. That is not a wasted poll; it spends the weekly allowance.

**ETags.** Cheaper to lose: a missing ETag costs one full GET per endpoint.
It lives here because it is the same table shape and the same lifetime.

**The queue.** Accepted triggers waiting for a worker, and the per-pull-request
leases that stop two workers reviewing one pull request at once. The table is
declared here because this module owns the schema; the claim protocol that
operates on it lives in :mod:`pr_review_agent.queue`.

**The ledger.** Every reservation the budget governor takes and every run it
settles. Unlike the other three it is a *record*, not a cache: the rolling
windows are computed from it, so its rows are append-only and are never
pruned -- not even when review content is purged after a merge. Deleting one
would silently hand back allowance that was genuinely spent. The arithmetic
over it lives in :mod:`pr_review_agent.budget`.

A watermark only ever moves forward. A restart that read a stale row, or two
cycles settling out of order, must not walk it backwards and re-admit events
that were already decided -- so :meth:`SqliteStore.advance_watermark` takes
the later of the stored and the offered value.

WAL mode is on because the daemon's later phases (queue, lease, budget
governor) read this file while the poller writes it. The write lock stays
single-writer either way, which is the property the budget reservation
depends on.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Applied in order; the file's ``user_version`` records how many have run.
#
# The ``CREATE`` statements are all ``IF NOT EXISTS`` because a database
# created before this list existed already carries the first migration's
# tables at ``user_version = 0``, and would otherwise fail to adopt it.
#
# They no longer have to be idempotent for crash-safety: ``_migrate`` applies
# each script and its version bump in one transaction, so a crash rolls the
# pair back together. ``ALTER TABLE ADD COLUMN`` has no ``IF NOT EXISTS``
# form in SQLite and could not have been written any other way.
_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS etags (
        path TEXT PRIMARY KEY,
        etag TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS watermarks (
        name TEXT PRIMARY KEY,
        at   TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS queue (
        dedupe_key   TEXT PRIMARY KEY,
        kind         TEXT NOT NULL,
        repo         TEXT NOT NULL,
        pr_number    INTEGER NOT NULL,
        head_sha     TEXT,
        actor_id     INTEGER NOT NULL,
        status       TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        enqueued_at  TEXT NOT NULL,
        leased_until TEXT,
        owner        TEXT
    );
    CREATE INDEX IF NOT EXISTS queue_claimable ON queue (status, enqueued_at);
    CREATE INDEX IF NOT EXISTS queue_by_pr ON queue (repo, pr_number, status);
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key       TEXT NOT NULL,
        owner            TEXT NOT NULL,
        actor_id         INTEGER NOT NULL,
        mode             TEXT NOT NULL,
        reserved_tokens  INTEGER NOT NULL,
        used_tokens      INTEGER,
        usage_confidence TEXT,
        engine           TEXT,
        model            TEXT,
        reserved_at      TEXT NOT NULL,
        settled_at       TEXT
    );
    CREATE INDEX IF NOT EXISTS ledger_window ON ledger (reserved_at);
    """,
    # The per-contributor window measures one ``actor_id`` over a trailing
    # duration, which is the first query to select on anything but time.
    """
    CREATE INDEX IF NOT EXISTS ledger_by_actor ON ledger (actor_id, reserved_at);
    """,
    """
    ALTER TABLE ledger ADD COLUMN reviewed_lines INTEGER;
    """,
    # Why a run stopped, which is a different question from how far its
    # recorded cost can be trusted. A run killed on the wall clock and one
    # whose envelope would not parse both settle `unavailable` at the full
    # reservation, so without this column nothing says which control bound
    # the run.
    """
    ALTER TABLE ledger ADD COLUMN stop_reason TEXT;
    """,
    # What the publisher acknowledges on. Both are NULL for a `pr_opened`
    # row, and for any row enqueued before this migration -- the publisher
    # falls back to reacting on the pull request rather than guessing an id.
    """
    ALTER TABLE queue ADD COLUMN comment_id INTEGER;
    ALTER TABLE queue ADD COLUMN comment_source TEXT;
    """,
    # What a paid review produced. The only table holding review content,
    # and therefore the only one the retention sweep purges; the ledger's
    # metrics survive that purge because they live elsewhere. See runs.py.
    """
    CREATE TABLE IF NOT EXISTS runs (
        dedupe_key        TEXT PRIMARY KEY,
        repo              TEXT NOT NULL,
        pr_number         INTEGER NOT NULL,
        head_sha          TEXT NOT NULL,
        outcome           TEXT NOT NULL,
        findings          TEXT NOT NULL,
        comment_id        INTEGER,
        recorded_at       TEXT NOT NULL,
        published_at      TEXT,
        content_purged_at TEXT
    );
    CREATE INDEX IF NOT EXISTS runs_by_pr ON runs (repo, pr_number);
    """,
    # The circuit breaker's three scalars. A separate table from `ledger`
    # because ledger rows are tokens genuinely consumed and the rolling
    # windows sum them; breaker state is not usage and must not be summed.
    """
    CREATE TABLE IF NOT EXISTS budget_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # The shared budget policy: whose configuration governs the pool when
    # several daemons share this file. One row, held there by the CHECK,
    # because a second row would be a second opinion about one allowance --
    # and two daemons that disagree do not split the pool between them, they
    # hand it to whichever was configured most permissively.
    #
    # Separate from `budget_state` even though both are one-row-ish scalars:
    # that one is what the breaker *learned*, this is what an operator
    # *declared*, and a reset of either must not touch the other.
    """
    CREATE TABLE IF NOT EXISTS budget_policy (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        authority_repo TEXT NOT NULL,
        policy         TEXT NOT NULL,
        written_at     TEXT NOT NULL
    );
    """,
)

SCHEMA_VERSION = len(_MIGRATIONS)


class SqliteStore:
    """Persistent ETags, watermarks and review queue for one daemon instance."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # A reader that meets the single write lock should wait for it rather
        # than raise "database is locked" on the spot.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- Schema -----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """How many migrations this database has had applied."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        """Apply every migration this database has not seen yet.

        Each script and its version bump commit together. ``executescript``
        would be the natural way to run a multi-statement script, but it
        issues a ``COMMIT`` of its own first, which would split the pair --
        so the statements are executed individually inside one transaction
        instead. A crash mid-migration therefore rolls back to the previous
        version and the migration is simply re-applied, rather than needing
        every statement to be independently idempotent.
        """
        for index, script in enumerate(
            _MIGRATIONS[self.schema_version :], start=self.schema_version + 1
        ):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in script.split(";"):
                    if statement.strip():
                        self._conn.execute(statement)
                # PRAGMA does not accept a bound parameter; `index` is a loop
                # counter over a module constant, never user input.
                self._conn.execute(f"PRAGMA user_version = {index:d}")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one ``BEGIN IMMEDIATE`` write transaction.

        The write lock is taken up front rather than on first write, which is
        what makes a read-then-write sequence -- such as the queue's
        conditional claim -- atomic against another writer. The budget
        reservation is specified to join this same transaction, so that a
        claim and the allowance it spends commit together or not at all.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- ETag cache (satisfies poller.etag_store.ETagCache) ---------------

    def get(self, path: str) -> str | None:
        """The last ETag seen for ``path``, or ``None`` on a cold start."""
        row = self._conn.execute(
            "SELECT etag FROM etags WHERE path = ?", (path,)
        ).fetchone()
        return None if row is None else row[0]

    def set(self, path: str, etag: str | None) -> None:
        """Record ``etag`` for ``path``; a ``None`` etag forgets the entry."""
        if etag is None:
            self._conn.execute("DELETE FROM etags WHERE path = ?", (path,))
        else:
            self._conn.execute(
                "INSERT INTO etags (path, etag) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET etag = excluded.etag",
                (path, etag),
            )

    # -- Watermarks -------------------------------------------------------

    def watermark(self, name: str) -> datetime | None:
        """The stored watermark for ``name``, as an aware UTC datetime."""
        row = self._conn.execute(
            "SELECT at FROM watermarks WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else parse_timestamp(row[0])

    def advance_watermark(self, name: str, at: datetime) -> datetime:
        """Move the ``name`` watermark forward to ``at``, never backwards.

        Returns the watermark in force afterwards, which is the later of the
        stored and the offered value.
        """
        at = to_utc(at, "watermark")
        current = self.watermark(name)
        if current is not None and current >= at:
            return current
        self._conn.execute(
            "INSERT INTO watermarks (name, at) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET at = excluded.at",
            (name, at.isoformat()),
        )
        return at

    # -- Shared budget policy ---------------------------------------------

    def budget_policy(self) -> BudgetPolicy | None:
        """The published policy, or ``None`` if no authority has run yet."""
        return read_budget_policy(self._conn)

    def publish_budget_policy(self, policy: BudgetPolicy, *, now: datetime) -> None:
        """Replace the published policy with ``policy``.

        Unconditional, so an authority restarting or reloading republishes
        rather than having to reconcile: its file is the declared truth, and
        the row is only ever a copy of it.
        """
        self._conn.execute(
            "INSERT INTO budget_policy (id, authority_repo, policy, written_at) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET authority_repo = excluded.authority_repo, "
            "policy = excluded.policy, written_at = excluded.written_at",
            (
                policy.authority_repo,
                json.dumps(policy.fields, sort_keys=True),
                to_utc(now, "written_at").isoformat(),
            ),
        )


@dataclass(frozen=True)
class BudgetPolicy:
    """The pool arithmetic one daemon published for the others to adopt."""

    authority_repo: str
    fields: dict[str, int | None]


def read_budget_policy(conn: sqlite3.Connection) -> BudgetPolicy | None:
    """The published policy, read on a caller's connection.

    Takes a connection rather than a store so the governor can read it inside
    the transaction its reservation is already being written in, which is what
    lets an authority's change reach a running complier with no signal to it.
    """
    row = conn.execute(
        "SELECT authority_repo, policy FROM budget_policy WHERE id = 1"
    ).fetchone()
    return None if row is None else BudgetPolicy(row[0], json.loads(row[1]))


def to_utc(value: datetime, what: str) -> datetime:
    """Normalise an aware datetime to UTC, rejecting a naive one.

    A naive datetime is refused at the boundary for the same reason
    ``Classifier.since`` refuses one: compared against a GitHub ``...Z``
    timestamp it raises ``TypeError`` at the worst possible moment.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    """Read back a timestamp written by :func:`to_utc`."""
    return datetime.fromisoformat(value).astimezone(timezone.utc)
