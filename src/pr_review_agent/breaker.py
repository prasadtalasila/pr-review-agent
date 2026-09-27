"""The only feedback from reality into the operator's guess.

Every limit in :mod:`pr_review_agent.budget` is a number somebody chose,
because the plan publishes no quota. A guess that is too low is harmless. A
guess that is too high is the one failure the rest of that design cannot
see: the governor admits runs and reports healthy utilisation while the real
limit is already being hit, and the worker retries into the same wall until
``max_attempts`` runs out, spending each time.

So a usage-limit failure is treated as evidence of two different facts, and
this module holds both. That the account is out **now** -- so nothing is
admitted for ``TRIP_HOLD``. And that the configured limits were too high --
so the calibration decays, and every window's effective limit with it.

**Multiplicative down, additive up.** A trip multiplies the calibration by
``DECAY_FACTOR``; a clean ``RECOVERY_PERIOD`` adds ``RECOVERY_POINTS`` back.
The asymmetry is what makes the series converge downward instead of
oscillating. Recovery has to exist at all because the usage pool is
*shared*: a trip does not always mean the operator's guess was too high, it
can equally mean the maintainer had a heavy week, and a calibration that
could only ever be revised downward would leave the reviewer permanently
crippled by one of those.

It lives beside the governor rather than inside it because the two answer
different questions. The governor measures windows and decides what may be
spent; this decides how far to trust the numbers those windows are measured
against. Splitting them is also what keeps either module small enough to
read in one sitting -- see AGENTS.md on the file limit.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ._time import parse
from .config.budget import SESSION

#: How long a trip refuses every claim. ``SESSION`` because it is the
#: shortest window, and because it is a *duration* rather than a reset time --
#: which is the only kind of answer available when the plan publishes none.
#: If the weekly limit was the one that blew, the next attempt trips again and
#: the calibration keeps shrinking, so the design converges either way rather
#: than needing the attribution to be right.
TRIP_HOLD = SESSION

#: What one trip does to the calibration, and what one clean window undoes.
DECAY_FACTOR = 0.9
RECOVERY_POINTS = 1
RECOVERY_PERIOD = SESSION

#: Percentage points, never a float: no drift across restarts, and the same
#: arithmetic ``BudgetConfig._share`` already uses. Floored at 1 rather than
#: 0 for the reason ``reviewer_share_pct`` is -- ``Governor._headroom``
#: divides by ``window.limit``, so a calibration reaching zero is a crash,
#: not a policy.
FULL_CALIBRATION = 100
MIN_CALIBRATION = 1

#: The breaker's whole state: three unrelated scalars, so a key/value table
#: rather than a row of one thing. Absent means never tripped, which is what
#: lets an existing database adopt migration 6 with no backfill.
_STATE_GET = "SELECT key, value FROM budget_state"
_STATE_SET = """
INSERT INTO budget_state (key, value) VALUES (:key, :value)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""


@dataclass(frozen=True)
class Breaker:
    """What the last usage-limit failure left behind.

    The defaults are "never tripped", which is what an empty
    ``budget_state`` reads back as -- so a database that predates migration 6
    needs no backfill to mean the right thing.
    """

    calibrated_pct: int = FULL_CALIBRATION
    tripped_until: datetime | None = None
    last_trip_at: datetime | None = None

    def tripped(self, now: datetime) -> bool:
        """Whether claims are still being refused outright."""
        return self.tripped_until is not None and now < self.tripped_until

    def calibration(self, now: datetime) -> int:
        """The stored calibration with accrued recovery applied.

        Computed rather than stored, so nothing is written on a read path and
        the whole rule is a pure function of three values.

        Because ``RECOVERY_PERIOD`` equals ``TRIP_HOLD``, the first point
        accrues exactly as the hold expires.
        """
        if self.last_trip_at is None:
            return self.calibrated_pct
        periods = (now - self.last_trip_at) // RECOVERY_PERIOD
        return min(
            FULL_CALIBRATION, self.calibrated_pct + RECOVERY_POINTS * max(0, periods)
        )


def decay(calibration: int) -> int:
    """What one trip leaves of ``calibration``.

    Truncated rather than rounded, so every trip is a strict decrease:
    rounding stalls at 4, where ``round(3.6)`` is 4 again and the
    calibration stops converging short of its floor.
    """
    return max(MIN_CALIBRATION, int(calibration * DECAY_FACTOR))


def calibrated(limit: int, calibration: int) -> int:
    """``limit`` scaled by what the breaker has learned.

    Never below one token: ``Governor._headroom`` divides by this, and a
    small configured window against a heavily decayed calibration would
    otherwise reach zero and raise where it should refuse.
    """
    return max(1, limit * calibration // FULL_CALIBRATION)


def read(conn: sqlite3.Connection) -> Breaker:
    """Read the breaker's state; every key absent means never tripped."""
    stored = dict(conn.execute(_STATE_GET).fetchall())
    tripped_until = stored.get("tripped_until")
    last_trip_at = stored.get("last_trip_at")
    return Breaker(
        calibrated_pct=int(stored.get("calibrated_pct", FULL_CALIBRATION)),
        tripped_until=None if tripped_until is None else parse(tripped_until),
        last_trip_at=None if last_trip_at is None else parse(last_trip_at),
    )


def write(conn: sqlite3.Connection, **values: str) -> None:
    """Write the breaker's state, inside the caller's transaction."""
    for key, value in values.items():
        conn.execute(_STATE_SET, {"key": key, "value": value})
