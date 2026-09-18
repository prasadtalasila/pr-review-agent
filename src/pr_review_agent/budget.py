"""The spending rails: nothing reaches a review engine except through here.

The agent spends a shared, metered allowance. All Claude surfaces draw on one
pool, so an unbounded reviewer does not merely overspend -- it locks the host
operator out of their own interactive sessions until the window resets. That
is why the governor lands *before* the review worker, and why this module is
the single gate a claim has to pass.

**Windows, one shape.** Every limit is tokens recorded in the ledger within a
trailing duration: five hours, seven days, and one day capped at a seventh of
the week. The effective ceiling is the tightest of them, and the ladder rung
comes from the worst utilisation among them. Each is a share of the *plan's*
limit rather than all of it -- ``reviewer_share_pct``, default 40 -- so a
runaway agent can degrade interactive Claude Code but cannot lock a maintainer
out of it.

**The fourth window is one contributor's.** When ``per_contributor_pct`` is
configured, a claim is also weighed against what its own ``actor_id`` has
spent over the weekly duration. It joins the same list, so it degrades to
mention-only and then refuses on the same ladder -- but only for the
contributor being admitted: everyone else's headroom is measured separately,
which is the point. Unset, the window is not built at all and nothing about
the other three changes.

**Reserve, then settle.** Checking the remaining allowance is not enough under
concurrency: two workers can each observe sufficient budget, each start a run,
and collectively breach the cap while both checked correctly. So the
reservation is written inside the *same* ``BEGIN IMMEDIATE`` transaction as
the queue claim, which SQLite's single write lock makes atomic for free. One
query measures a window and serves both settled history and live holds::

    SELECT COALESCE(SUM(COALESCE(used_tokens, reserved_tokens)), 0)

A settled row counts what it spent; an unsettled one counts its full
reservation. That fallback is the whole concurrency guarantee -- the second
worker sees the first's reservation as already spent, before the first has
finished.

**A crashed worker's reservation stays charged** until it ages out of its
rolling window. BUDGET.md says a reservation expires with the lease so a
crashed worker "does not leak allowance", and this honours the *intent* of
that rather than its letter: a crashed run has probably already spent tokens,
and ``DEFAULT_MAX_ATTEMPTS`` is 3, so releasing on expiry would let one
trigger spend invisibly three times over. Not leaking means not held forever,
which a rolling window guarantees; it does not mean granting an amnesty. The
pessimistic direction is the safe one for a spending control, and it is also
why the ledger needs no expiry column and no sweep.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from ._compat import StrEnum
from .config import BudgetConfig
from .queue import Claim
from .store import SqliteStore, to_utc
from .triggers.models import TriggerKind

logger = logging.getLogger(__name__)

#: The rolling windows, in the order they are reported. ``DAILY`` is a flat
#: seventh of the weekly limit rather than ``weekly_remaining / days_left``:
#: a rolling window never resets, so "days remaining" has no value, and
#: anchoring one would mean inventing a reset day the plan does not publish.
#: "At most a seventh of the week in any day" needs no anchor and is stricter
#: -- an agent idle since Monday cannot burn four days' allowance on Friday.
SESSION = timedelta(hours=5)
WEEKLY = timedelta(days=7)
DAILY = timedelta(days=1)

#: The ladder. Below the first, everything is admitted; at the first, fresh
#: pull requests stop being auto-reviewed so the remainder is conserved for a
#: pull request somebody explicitly asks about; at the second, nothing runs.
MENTION_ONLY_AT = 0.85
EXHAUSTED_AT = 1.0


class Mode(StrEnum):
    """Which rung of the degradation ladder a run was admitted under."""

    FULL = "full"
    MENTION_ONLY = "mention_only"
    EXHAUSTED = "exhausted"


class UsageConfidence(StrEnum):
    """How much the recorded token usage can be trusted.

    Some engines report no usage at all, which forces the governor onto proxy
    controls -- run count and wall clock -- and that is a materially weaker
    guarantee than a token count. Recording which one applied is what lets an
    operator see the difference afterwards.
    """

    EXACT = "exact"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Window:
    """One rolling limit: ``limit`` tokens within a trailing ``duration``.

    ``actor_id`` scopes the measurement to a single contributor. It is set
    only on the contributor window, which is therefore built per claim
    rather than once per configuration.
    """

    name: str
    duration: timedelta
    limit: int
    actor_id: int | None = None


@dataclass(frozen=True)
class Usage:
    """What a finished run reported, as recorded on its ledger row.

    The four travel together because they are one answer -- "what did this
    run cost, and how much do we trust that" -- and because the ledger row is
    what a posted comment has to be traceable back to.
    """

    tokens: int
    confidence: UsageConfidence
    engine: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class Headroom:
    """What the windows allow right now, and which one binds."""

    mode: Mode
    remaining: int
    tightest: str


_RESERVE = """
INSERT INTO ledger
    (dedupe_key, owner, actor_id, mode, reserved_tokens, reserved_at)
VALUES (:key, :owner, :actor, :mode, :tokens, :now)
"""

# An unsettled row counts its whole reservation, which is what stops a second
# worker in the next transaction from spending allowance the first has
# already committed to.
_USED_SINCE = """
SELECT COALESCE(SUM(COALESCE(used_tokens, reserved_tokens)), 0)
FROM ledger WHERE reserved_at > :start
"""

# The contributor window, served by the `ledger_by_actor` index.
_USED_SINCE_BY_ACTOR = _USED_SINCE + " AND actor_id = :actor"

# Guarded on the owner, exactly as ``queue._FINISH`` is: a worker whose lease
# lapsed and was re-claimed must not settle the row the newer worker holds.
_SETTLE = """
UPDATE ledger
SET used_tokens = :used, usage_confidence = :confidence,
    engine = :engine, model = :model, settled_at = :now
WHERE dedupe_key = :key AND owner = :owner AND settled_at IS NULL
"""


class Governor:
    """The one gate between a queued trigger and a review engine."""

    def __init__(self, store: SqliteStore, config: BudgetConfig) -> None:
        self._store = store
        self._config = config
        self._windows = _windows(config)

    @property
    def config(self) -> BudgetConfig:
        """The budget settings currently in force."""
        return self._config

    def reload(self, config: BudgetConfig) -> None:
        """Adopt ``config``, so ``SIGHUP`` needs no restart to take effect."""
        self._config = config
        self._windows = _windows(config)

    def admit(self, conn: sqlite3.Connection, claim: Claim, now: datetime) -> bool:
        """Reserve this run's ceiling, or refuse it.

        Runs inside the transaction :meth:`~pr_review_agent.queue.ReviewQueue.claim`
        opened, so the reservation and the claim commit together or not at
        all. Never opens or closes a transaction of its own.
        """
        if not self._config.enabled:
            logger.warning(
                "budget.enabled is false: refusing %s", claim.trigger.dedupe_key
            )
            return False
        headroom = self._headroom(conn, now, actor_id=claim.trigger.actor_id)
        if not self._allows(headroom, claim):
            return False
        conn.execute(
            _RESERVE,
            {
                "key": claim.trigger.dedupe_key,
                "owner": claim.owner,
                "actor": claim.trigger.actor_id,
                "mode": str(headroom.mode),
                "tokens": self._config.max_run_tokens,
                "now": _stamp(now),
            },
        )
        return True

    def settle(self, claim: Claim, usage: Usage, *, now: datetime) -> bool:
        """Record what the run actually spent, releasing the remainder.

        ``False`` when no unsettled reservation is held for this claim, which
        is how a worker whose lease lapsed learns to discard its result.
        """
        with self._store.transaction() as conn:
            return (
                conn.execute(
                    _SETTLE,
                    {
                        "used": usage.tokens,
                        "confidence": str(usage.confidence),
                        "engine": usage.engine,
                        "model": usage.model,
                        "now": _stamp(now),
                        "key": claim.trigger.dedupe_key,
                        "owner": claim.owner,
                    },
                ).rowcount
                == 1
            )

    def headroom(self, now: datetime) -> Headroom:
        """What the shared windows allow, for an operator or a status readout.

        Deliberately actor-agnostic: a readout has no contributor to scope
        to, so the per-contributor window belongs to the admission path.
        """
        with self._store.transaction() as conn:
            return self._headroom(conn, now)

    def _headroom(
        self, conn: sqlite3.Connection, now: datetime, actor_id: int | None = None
    ) -> Headroom:
        """The worst utilisation and the tightest remainder across windows."""
        at = to_utc(now, "now")
        windows = self._windows + self._contributor_window(actor_id)
        worst, tightest = 0.0, windows[0]
        remaining = None
        for window in windows:
            used = _used_since(conn, at - window.duration, window.actor_id)
            worst = max(worst, used / window.limit)
            left = window.limit - used
            if remaining is None or left < remaining:
                remaining, tightest = left, window
        assert remaining is not None  # the window list is never empty
        return Headroom(
            mode=_mode_for(worst), remaining=max(0, remaining), tightest=tightest.name
        )

    def _contributor_window(self, actor_id: int | None) -> tuple[Window, ...]:
        """The fourth window, when one contributor's claim is being weighed.

        Empty unless ``per_contributor_pct`` is configured *and* an actor is
        being admitted, which is why an unset cap changes nothing at all.
        """
        limit = self._config.per_contributor_limit
        if actor_id is None or limit is None:
            return ()
        return (Window("contributor", WEEKLY, limit, actor_id=actor_id),)

    def _allows(self, headroom: Headroom, claim: Claim) -> bool:
        """Whether the ladder and the remaining allowance permit this run."""
        key = claim.trigger.dedupe_key
        if headroom.mode is Mode.EXHAUSTED:
            logger.warning(
                "budget exhausted on the %s window: refusing %s",
                headroom.tightest,
                key,
            )
            return False
        if (
            headroom.mode is Mode.MENTION_ONLY
            and claim.trigger.kind is not TriggerKind.MENTION
        ):
            logger.warning(
                "budget at %d%% of the %s window: auto-review paused, refusing %s",
                int(MENTION_ONLY_AT * 100),
                headroom.tightest,
                key,
            )
            return False
        if headroom.remaining < self._config.max_run_tokens:
            # No partial reservation: a run that cannot be afforded in full is
            # not worth starting, because a truncated review is still a spend.
            logger.warning(
                "the %s window has %d tokens left, below the %d a run reserves: "
                "refusing %s",
                headroom.tightest,
                headroom.remaining,
                self._config.max_run_tokens,
                key,
            )
            return False
        return True


def _windows(config: BudgetConfig) -> tuple[Window, ...]:
    """The three rolling windows ``config`` describes."""
    return (
        Window("session", SESSION, config.session_limit),
        Window("weekly", WEEKLY, config.weekly_limit),
        Window("daily", DAILY, config.daily_limit),
    )


def _used_since(
    conn: sqlite3.Connection, start: datetime, actor_id: int | None = None
) -> int:
    """Tokens committed -- spent or reserved -- since ``start``.

    Scoped to one contributor when ``actor_id`` is given, which is the whole
    of the per-contributor window: the same arithmetic, a narrower ``WHERE``.
    """
    params: dict[str, object] = {"start": _stamp(start)}
    if actor_id is None:
        return int(conn.execute(_USED_SINCE, params).fetchone()[0])
    params["actor"] = actor_id
    return int(conn.execute(_USED_SINCE_BY_ACTOR, params).fetchone()[0])


def _mode_for(utilisation: float) -> Mode:
    """The ladder rung for the worst window utilisation."""
    if utilisation >= EXHAUSTED_AT:
        return Mode.EXHAUSTED
    if utilisation >= MENTION_ONLY_AT:
        return Mode.MENTION_ONLY
    return Mode.FULL


def _stamp(value: datetime) -> str:
    """Format a timestamp for storage and for comparison inside SQL.

    The same fixed-width aware-UTC form ``queue._stamp`` writes, so ISO-8601
    sorts lexicographically in the order the instants occur and a window's
    start can be a plain SQL comparison.
    """
    return to_utc(value, "timestamp").isoformat()
