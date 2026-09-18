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

**The pre-flight estimate is the last free refusal.** ``preflight`` predicts
a run's cost from the lines that survived ``budget.excluded_paths`` and
refuses one that cannot fit ``max_run_tokens`` -- releasing the reservation
in the same call, because nothing else releases one early. The rate is fitted
against settled ledger rows, falling back to a deliberately high constant
until there are enough of them to fit.

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

#: The pre-flight estimate's cold start. A fresh database has no history to
#: fit a rate against, and the first runs are exactly when an over-estimate
#: is cheapest to get wrong -- so the fallback errs high. Forty is above what
#: a review is expected to cost per changed line, which over-refuses rather
#: than overspends; against the shipped ``max_run_tokens`` it puts the
#: refusal threshold at 1,500 reviewable lines.
#:
#: Neither is configurable. An operator's escape hatch is ``max_run_tokens``,
#: which they already have to choose; a second knob multiplying into it is
#: CONFIG.md's failure mode rather than a feature.
DEFAULT_TOKENS_PER_LINE = 40

#: Settled, measurable runs needed before the fit replaces the constant.
MIN_FIT_SAMPLES = 10


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

# The rung ``admit`` recorded, read back for the run it admitted. Same guard
# as ``_SETTLE``, because it answers the same question: is this reservation
# still ours?
_ADMITTED_MODE = """
SELECT mode FROM ledger
WHERE dedupe_key = :key AND owner = :owner AND settled_at IS NULL
"""

# Guarded on the owner, exactly as ``queue._FINISH`` is: a worker whose lease
# lapsed and was re-claimed must not settle the row the newer worker holds.
_SETTLE = """
UPDATE ledger
SET used_tokens = :used, usage_confidence = :confidence,
    engine = :engine, model = :model, reviewed_lines = :lines, settled_at = :now
WHERE dedupe_key = :key AND owner = :owner AND settled_at IS NULL
"""

# What the pre-flight estimate is fitted against. Only settled rows that
# actually reviewed something, and only where the engine reported a real
# token count: `estimated` and `unavailable` rows describe a run the governor
# could not measure, and fitting a rate to them would turn a known-weaker
# guarantee into a confidently wrong number.
_FIT_SAMPLE = """
SELECT COUNT(*), COALESCE(SUM(used_tokens), 0), COALESCE(SUM(reviewed_lines), 0)
FROM ledger
WHERE settled_at IS NOT NULL AND usage_confidence = :exact
  AND reviewed_lines IS NOT NULL AND reviewed_lines > 0
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

    def settle(
        self,
        claim: Claim,
        usage: Usage,
        *,
        now: datetime,
        reviewed_lines: int | None = None,
    ) -> bool:
        """Record what the run actually spent, releasing the remainder.

        ``False`` when no unsettled reservation is held for this claim, which
        is how a worker whose lease lapsed learns to discard its result.

        ``reviewed_lines`` is what the pre-flight estimate is fitted against,
        and it is a separate argument rather than a field on ``Usage`` on
        purpose. ``ReviewResult.usage`` *is* this ``Usage``, so putting it
        there would make an adapter responsible for reporting the size it
        was handed -- and an adapter that under-reported would bias the rate
        downward, which is a spending control taking its input from the
        thing it controls. The worker reads it off ``Checkout.reviewed``.
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
                        "lines": reviewed_lines,
                        "now": _stamp(now),
                        "key": claim.trigger.dedupe_key,
                        "owner": claim.owner,
                    },
                ).rowcount
                == 1
            )

    def estimate(self, reviewed_lines: int) -> int:
        """Predicted tokens for a review of ``reviewed_lines`` lines.

        ``rate x lines``, with no fitted intercept. A real review has a fixed
        overhead -- the prompt, the instructions, the first file read -- and
        a two-parameter fit would capture it, but the intercept is unstable
        on a handful of samples and the only question asked here is whether
        a pull request is *large* enough to refuse. Under-predicting a fifty
        line change is harmless, because a fifty line change is nowhere near
        the cap. The fixed cost is amortised into the rate instead, where it
        makes large diffs predict slightly high: the safe direction.
        """
        return round(self._rate() * reviewed_lines)

    def preflight(self, claim: Claim, reviewed_lines: int, now: datetime) -> bool:
        """Whether this run is worth starting -- releasing its hold if not.

        Refuses a pull request predicted to cost more than one run may spend,
        and one with nothing left to review after ``budget.excluded_paths``.
        Both are free refusals: no engine has run, and no tokens are gone.

        **The release happens inside this call, not beside it.** A caller
        that refused and forgot to settle would leave a full reservation
        charged against every window until it aged out, because nothing
        releases a reservation early by design. Making the decision and the
        release one call means that failure cannot be introduced by a
        caller.
        """
        key = claim.trigger.dedupe_key
        if reviewed_lines <= 0:
            logger.info(
                "nothing left to review in %s after path exclusions: refusing", key
            )
        else:
            predicted = self.estimate(reviewed_lines)
            if predicted <= self._config.max_run_tokens:
                return True
            logger.warning(
                "%s is predicted to cost %d tokens over %d reviewable lines, "
                "above the %d a run may spend: refusing",
                key,
                predicted,
                reviewed_lines,
                self._config.max_run_tokens,
            )
        # Known to have cost nothing, which is not the same as unknown: an
        # `unavailable` row would draw its whole reservation down instead.
        self.settle(
            claim,
            Usage(tokens=0, confidence=UsageConfidence.EXACT),
            now=now,
        )
        return False

    def _rate(self) -> float:
        """Tokens per reviewable line, fitted against the ledger.

        Below ``MIN_FIT_SAMPLES`` the fit has nothing to say, so the
        documented constant stands in. It is deliberately above what a review
        is expected to cost: erring high over-refuses rather than overspends,
        and a fresh database is exactly where an over-estimate is cheapest to
        get wrong.

        Once the sample exists the fit takes over outright, with no floor
        under it. A rate that could only ever be revised upward would refuse
        pull requests the agent has direct evidence it can afford, which is
        not a fit.
        """
        with self._store.transaction() as conn:
            rows, tokens, lines = conn.execute(
                _FIT_SAMPLE, {"exact": str(UsageConfidence.EXACT)}
            ).fetchone()
        if rows < MIN_FIT_SAMPLES or not lines:
            return float(DEFAULT_TOKENS_PER_LINE)
        return tokens / lines

    def admitted_mode(self, claim: Claim) -> Mode | None:
        """The ladder rung ``claim``'s unsettled reservation was admitted under.

        The worker needs it to fill ``ReviewRequest.mode``, and ``Claim``
        cannot carry it: :mod:`pr_review_agent.queue` imports nothing from
        here, which is what keeps the budget out of the claim signature. So
        it is read back from the row ``admit`` already wrote.

        ``None`` when no unsettled reservation is held for this claim --
        guarded on the owner exactly as :meth:`settle` is, so a worker whose
        lease lapsed learns to discard the run before starting it.
        """
        with self._store.transaction() as conn:
            row = conn.execute(
                _ADMITTED_MODE,
                {"key": claim.trigger.dedupe_key, "owner": claim.owner},
            ).fetchone()
        return None if row is None else Mode(row[0])

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
