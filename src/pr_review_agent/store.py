"""The SQLite state that has to survive a restart.

Two things are kept here, both of them about *not repeating work*:

**Watermarks.** The poller sees open pull requests and recent comments, not
``opened`` events, so the classifier needs a timestamp below which everything
has already been considered. Held only in memory, a restart re-offers the
whole open backlog -- and, for comments, replays every old ``@claude`` as a
fresh request. That is not a wasted poll; it spends the weekly allowance.

**ETags.** Cheaper to lose: a missing ETag costs one full GET per endpoint.
It lives here because it is the same table shape and the same lifetime.

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
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS etags (
    path TEXT PRIMARY KEY,
    etag TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
    name TEXT PRIMARY KEY,
    at   TEXT NOT NULL
);
"""


class SqliteStore:
    """Persistent ETag cache and watermark table for one daemon instance."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

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
        return None if row is None else _parse(row[0])

    def advance_watermark(self, name: str, at: datetime) -> datetime:
        """Move the ``name`` watermark forward to ``at``, never backwards.

        Returns the watermark in force afterwards, which is the later of the
        stored and the offered value.
        """
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("watermark must be timezone-aware (UTC)")
        at = at.astimezone(timezone.utc)
        current = self.watermark(name)
        if current is not None and current >= at:
            return current
        self._conn.execute(
            "INSERT INTO watermarks (name, at) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET at = excluded.at",
            (name, at.isoformat()),
        )
        return at


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)
