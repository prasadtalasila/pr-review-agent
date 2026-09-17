"""In-memory ETag cache, keyed by request path.

A cold start (no prior ETag) always performs a full GET. Persistence across
restarts belongs to the SQLite store landing with the queue and budget
governor; until then, a restart costs one extra full poll per endpoint, not
correctness.
"""

from __future__ import annotations


class ETagStore:
    """Remembers the last ETag seen for each polled path."""

    def __init__(self) -> None:
        self._etags: dict[str, str] = {}

    def get(self, path: str) -> str | None:
        return self._etags.get(path)

    def set(self, path: str, etag: str | None) -> None:
        if etag is None:
            self._etags.pop(path, None)
        else:
            self._etags[path] = etag
