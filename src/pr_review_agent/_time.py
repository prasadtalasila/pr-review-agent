"""The one clock, the one storage format, and the one interruptible sleep.

Every instant this package writes to SQLite goes through :func:`stamp`, and
every instant it reads back through :func:`parse`. Keeping both here is not
tidiness: the storage format carries an invariant that several modules
depend on and none of them could state alone.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

__all__ = ["now", "parse", "stamp", "to_utc", "wait_until"]


def now() -> datetime:
    """The current instant, aware and in UTC, as the store requires."""
    return datetime.now(timezone.utc)


def to_utc(value: datetime, what: str) -> datetime:
    """Normalise an aware datetime to UTC, rejecting a naive one.

    A naive datetime is refused at the boundary for the same reason
    ``Classifier.since`` refuses one: compared against a GitHub ``...Z``
    timestamp it raises ``TypeError`` at the worst possible moment.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def stamp(value: datetime, what: str = "timestamp") -> str:
    """Format a timestamp for storage, and for comparison *inside* SQL.

    Every value goes through :func:`to_utc` first, so each one carries the
    same ``+00:00`` suffix and the same widths down to the second. The
    fractional part is the one variable-width field, and it is harmless:
    ``+`` sorts before ``.``, so a whole second still precedes the same
    second with a fraction. ISO-8601 therefore sorts lexicographically in the
    order the instants occur, which is what lets the lease-expiry predicates
    and the budget windows be plain SQL comparisons rather than a
    read-and-compare in Python.

    ``what`` names the value in the error a naive datetime raises.
    """
    return to_utc(value, what).isoformat()


def parse(value: str) -> datetime:
    """Read back a timestamp written by :func:`stamp`, or one GitHub wrote.

    GitHub writes the ``Z`` suffix, which ``datetime.fromisoformat`` does not
    accept before Python 3.11; ``stamp`` writes ``+00:00``, which every
    supported version accepts. Both spellings reach this function -- the
    poller's payloads and the store's own rows -- so it takes both.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


async def wait_until(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or until ``stop`` is set -- whichever comes first.

    A plain sleep would make a ``SIGTERM`` arriving early in a 600 s idle
    interval hang a service restart for the remainder of it.
    """
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
