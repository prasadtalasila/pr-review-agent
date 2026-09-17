"""The SQLite state that has to survive a restart.

Three things are kept here, all of them about *not repeating work*:

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

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Applied in order; the file's ``user_version`` records how many have run.
#
# Every statement is ``IF NOT EXISTS`` for two reasons. A database created
# before this list existed already carries the first migration's tables at
# ``user_version = 0``, and would otherwise fail to adopt it. And a crash
# between ``executescript`` and the version bump must leave the migration
# re-runnable rather than wedged, which needs each one to be idempotent.
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
        """Apply every migration this database has not seen yet."""
        for index, script in enumerate(
            _MIGRATIONS[self.schema_version :], start=self.schema_version + 1
        ):
            self._conn.executescript(script)
            # PRAGMA does not accept a bound parameter; `index` is a loop
            # counter over a module constant, never user input.
            self._conn.execute(f"PRAGMA user_version = {index:d}")

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
