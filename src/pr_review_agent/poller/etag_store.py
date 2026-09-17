"""ETag caches, keyed by request path.

A cold start (no prior ETag) always performs a full GET, so losing the cache
costs one extra poll per endpoint, not correctness. ``ETagStore`` is the
in-memory cache used by tests and by a one-shot poll;
:class:`~pr_review_agent.store.SqliteStore` is the one the daemon uses, and
satisfies the same :class:`ETagCache` protocol.
"""

from __future__ import annotations

from typing import Protocol


class ETagCache(Protocol):
    """What the poller needs of an ETag cache."""

    def get(self, path: str) -> str | None:
        """The last ETag seen for ``path``, or ``None`` on a cold start."""

    def set(self, path: str, etag: str | None) -> None:
        """Record ``etag`` for ``path``; a ``None`` etag forgets the entry."""


class ETagStore:
    """Remembers the last ETag seen for each polled path, in memory only."""

    def __init__(self) -> None:
        self._etags: dict[str, str] = {}

    def get(self, path: str) -> str | None:
        """The last ETag seen for ``path``, or ``None`` on a cold start."""
        return self._etags.get(path)

    def set(self, path: str, etag: str | None) -> None:
        """Record ``etag`` for ``path``; a ``None`` etag forgets the entry."""
        if etag is None:
            self._etags.pop(path, None)
        else:
            self._etags[path] = etag
