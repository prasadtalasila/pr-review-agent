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
from .store import SqliteStore, parse_timestamp, read_budget_policy, to_utc
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

#: How long a trip refuses every claim. ``SESSION`` because it is the
#: shortest window, and because it is a *duration* rather than a reset time --
#: which is the only kind of answer available when the plan publishes none.
#: If the weekly limit was the one that blew, the next attempt trips again and
#: the calibration keeps shrinking, so the design converges either way rather
#: than needing the attribution to be right.
TRIP_HOLD = SESSION

#: What one trip does to the calibration, and what one clean window undoes.
#: Multiplicative down, additive up: the series converges instead of
#: oscillating, and a one-off heavy week on the *shared* pool heals rather
#: than crippling the reviewer for good.
DECAY_FACTOR = 0.9
RECOVERY_POINTS = 1
RECOVERY_PERIOD = SESSION

#: Percentage points, never a float: no drift across restarts, and the same
#: arithmetic ``BudgetConfig._share`` already uses. Floored at 1 rather than
#: 0 for the reason ``reviewer_share_pct`` is -- ``_headroom`` divides by
#: ``window.limit``, so a calibration reaching zero is a crash, not a policy.
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


class StopReason(StrEnum):
    """Why a run stopped, as recorded on its ledger row.

    ``UsageConfidence`` says how far the recorded cost can be trusted; this
    says what happened. They are different questions, and collapsing them is
    why a run killed on the wall clock and a run whose envelope would not
    parse were previously the same row -- both ``unavailable``, both charged
    the full reservation, and nothing to say which control had bound them.

    A bounded set rather than free text, because the column is read by an
    operator and, later, by the circuit breaker: ``GROUP BY stop_reason`` has
    to mean something.
    """

    COMPLETED = "completed"
    TRUNCATED = "truncated"
    FAILED = "failed"
    #: Killed by ``engine.timeout_seconds``. The only per-run ceiling the
    #: agent enforces today, so it is the one worth being able to count.
    TIMEOUT = "timeout"
    ENGINE_ERROR = "engine_error"
    #: The adapter's subprocess never started -- a missing binary, or a cwd
    #: that is not there. Settles at zero: no process existed, so nothing was
    #: spent, and that is provable in the way a killed run's spend is not.
    #: Kept apart from ``ENGINE_ERROR`` because the operator action differs:
    #: this one is the host's configuration, that one is the tool.
    ENGINE_UNAVAILABLE = "engine_unavailable"
    #: The pre-flight estimate refused the run. Settles at zero: no engine
    #: ran, so nothing was spent.
    REFUSED = "refused"
    #: GitHub or the workspace failed before the engine started.
    INFRASTRUCTURE = "infrastructure"
    #: The *account's* limit was reached, not this run's ceiling. The one
    #: reason that trips the circuit breaker rather than being retried.
    USAGE_LIMIT = "usage_limit"


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

        Recovery is additive against a multiplicative decay, which is what
        makes the series converge downward instead of oscillating. It has to
        exist at all because the usage pool is *shared*: a trip does not
        always mean the operator's guess was too high, it can equally mean
        the maintainer had a heavy week, and a calibration that could only
        ever be revised downward would leave the reviewer permanently
        crippled by one of those.

        Because ``RECOVERY_PERIOD`` equals ``TRIP_HOLD``, the first point
        accrues exactly as the hold expires.
        """
        if self.last_trip_at is None:
            return self.calibrated_pct
        periods = (now - self.last_trip_at) // RECOVERY_PERIOD
        return min(
            FULL_CALIBRATION, self.calibrated_pct + RECOVERY_POINTS * max(0, periods)
        )


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
    engine = :engine, model = :model, reviewed_lines = :lines,
    stop_reason = :reason, settled_at = :now
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

    def __init__(
        self, store: SqliteStore, config: BudgetConfig, *, comply: bool = False
    ) -> None:
        self._store = store
        self._config = config
        self._comply = comply

    @property
    def config(self) -> BudgetConfig:
        """This daemon's own budget settings, before any adoption."""
        return self._config

    def reload(self, config: BudgetConfig) -> None:
        """Adopt ``config``, so ``SIGHUP`` needs no restart to take effect."""
        self._config = config

    def _effective(self, conn: sqlite3.Connection) -> BudgetConfig:
        """The budget actually in force on ``conn``.

        For a complier this is the local file under the authority's published
        pool arithmetic, re-read on every admission rather than cached. That
        is what makes an authority's ``SIGHUP`` reach a running complier
        without signalling it: the next claim simply measures against the new
        numbers. It costs one single-row lookup on a connection the caller has
        already opened.

        A missing row leaves the local file standing. Startup refuses to run a
        complier before an authority has published, so this is the *last*
        published policy going missing under a live daemon rather than a state
        the daemon can start in.
        """
        if not self._comply:
            return self._config
        published = read_budget_policy(conn)
        if published is None:
            return self._config
        return self._config.adopt(published.fields)

    def _effective_now(self) -> BudgetConfig:
        """The same, for a caller that holds no transaction of its own."""
        if not self._comply:
            return self._config
        with self._store.transaction() as conn:
            return self._effective(conn)

    def admit(self, conn: sqlite3.Connection, claim: Claim, now: datetime) -> bool:
        """Reserve this run's ceiling, or refuse it.

        Runs inside the transaction :meth:`~pr_review_agent.queue.ReviewQueue.claim`
        opened, so the reservation and the claim commit together or not at
        all. Never opens or closes a transaction of its own.
        """
        config = self._effective(conn)
        if not config.enabled:
            logger.warning(
                "budget.enabled is false: refusing %s", claim.trigger.dedupe_key
            )
            return False
        at = to_utc(now, "now")
        breaker = _breaker(conn)
        if breaker.tripped(at):
            # Ahead of the window arithmetic on purpose: a trip is a fact
            # about the account, and the arithmetic is the guess it just
            # contradicted.
            logger.warning(
                "circuit breaker tripped until %s: refusing %s",
                breaker.tripped_until,
                claim.trigger.dedupe_key,
            )
            return False
        headroom = self._headroom(
            conn,
            now,
            config,
            actor_id=claim.trigger.actor_id,
            calibration=breaker.calibration(at),
        )
        if not self._allows(headroom, claim, config):
            return False
        conn.execute(
            _RESERVE,
            {
                "key": claim.trigger.dedupe_key,
                "owner": claim.owner,
                "actor": claim.trigger.actor_id,
                "mode": str(headroom.mode),
                "tokens": config.max_run_tokens,
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
        stop_reason: StopReason,
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

        ``stop_reason`` is required and has no default. A default would be a
        reason nobody gave, and the column exists precisely to remove that
        ambiguity -- a ``NULL`` here would mean the thing it was added to
        stop meaning, which is "something happened".
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
                        "reason": str(stop_reason),
                        "now": _stamp(now),
                        "key": claim.trigger.dedupe_key,
                        "owner": claim.owner,
                    },
                ).rowcount
                == 1
            )

    def trip(self, now: datetime) -> int:
        """Record that the account's real limit was hit; return the new calibration.

        Two things at once, because a usage-limit error is evidence of two
        different facts. That the account is out *now* -- so nothing is
        admitted for ``TRIP_HOLD``. And that the configured limits were too
        high -- so the calibration decays, and every window's effective limit
        with it.

        The decay applies to the *recovered* value, so recovery accrued since
        the previous trip is counted before it is undone.
        """
        at = to_utc(now, "now")
        with self._store.transaction() as conn:
            # Truncated rather than rounded, so every trip is a strict
            # decrease: rounding stalls at 4, where `round(3.6)` is 4 again
            # and the calibration stops converging short of its floor.
            calibrated = max(
                MIN_CALIBRATION, int(_breaker(conn).calibration(at) * DECAY_FACTOR)
            )
            _set_breaker(
                conn,
                calibrated_pct=str(calibrated),
                tripped_until=_stamp(at + TRIP_HOLD),
                last_trip_at=_stamp(at),
            )
        logger.warning(
            "usage limit reached: refusing every claim for %s, and the effective "
            "limits are now %d%% of the configured ones",
            TRIP_HOLD,
            calibrated,
        )
        return calibrated

    def breaker(self) -> Breaker:
        """The breaker's state, for an operator or a status readout."""
        with self._store.transaction() as conn:
            return _breaker(conn)

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
        max_run_tokens = self._effective_now().max_run_tokens
        if reviewed_lines <= 0:
            logger.info(
                "nothing left to review in %s after path exclusions: refusing", key
            )
        else:
            predicted = self.estimate(reviewed_lines)
            if predicted <= max_run_tokens:
                return True
            logger.warning(
                "%s is predicted to cost %d tokens over %d reviewable lines, "
                "above the %d a run may spend: refusing",
                key,
                predicted,
                reviewed_lines,
                max_run_tokens,
            )
        # Known to have cost nothing, which is not the same as unknown: an
        # `unavailable` row would draw its whole reservation down instead.
        self.settle(
            claim,
            Usage(tokens=0, confidence=UsageConfidence.EXACT),
            now=now,
            stop_reason=StopReason.REFUSED,
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
        at = to_utc(now, "now")
        with self._store.transaction() as conn:
            return self._headroom(
                conn,
                now,
                self._effective(conn),
                calibration=_breaker(conn).calibration(at),
            )

    def _headroom(
        self,
        conn: sqlite3.Connection,
        now: datetime,
        config: BudgetConfig,
        *,
        actor_id: int | None = None,
        calibration: int = FULL_CALIBRATION,
    ) -> Headroom:
        """The worst utilisation and the tightest remainder across windows.

        ``calibration`` is what the circuit breaker has learned, so every
        window is measured against ``min(configured, calibrated)`` rather
        than against the operator's guess alone.
        """
        at = to_utc(now, "now")
        windows = _windows(config) + self._contributor_window(config, actor_id)
        worst, tightest = 0.0, windows[0]
        remaining = None
        for window in windows:
            limit = _calibrated(window.limit, calibration)
            used = _used_since(conn, at - window.duration, window.actor_id)
            worst = max(worst, used / limit)
            left = limit - used
            if remaining is None or left < remaining:
                remaining, tightest = left, window
        assert remaining is not None  # the window list is never empty
        return Headroom(
            mode=_mode_for(worst), remaining=max(0, remaining), tightest=tightest.name
        )

    def _contributor_window(
        self, config: BudgetConfig, actor_id: int | None
    ) -> tuple[Window, ...]:
        """The fourth window, when one contributor's claim is being weighed.

        Empty unless ``per_contributor_pct`` is configured *and* an actor is
        being admitted, which is why an unset cap changes nothing at all.
        """
        limit = config.per_contributor_limit
        if actor_id is None or limit is None:
            return ()
        return (Window("contributor", WEEKLY, limit, actor_id=actor_id),)

    def _allows(self, headroom: Headroom, claim: Claim, config: BudgetConfig) -> bool:
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
        if headroom.remaining < config.max_run_tokens:
            # No partial reservation: a run that cannot be afforded in full is
            # not worth starting, because a truncated review is still a spend.
            logger.warning(
                "the %s window has %d tokens left, below the %d a run reserves: "
                "refusing %s",
                headroom.tightest,
                headroom.remaining,
                config.max_run_tokens,
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


def _calibrated(limit: int, calibration: int) -> int:
    """``limit`` scaled by what the breaker has learned.

    Never below one token: ``_headroom`` divides by this, and a small
    configured window against a heavily decayed calibration would otherwise
    reach zero and raise where it should refuse.
    """
    return max(1, limit * calibration // FULL_CALIBRATION)


def _breaker(conn: sqlite3.Connection) -> Breaker:
    """Read the breaker's state; every key absent means never tripped."""
    stored = dict(conn.execute(_STATE_GET).fetchall())
    tripped_until = stored.get("tripped_until")
    last_trip_at = stored.get("last_trip_at")
    return Breaker(
        calibrated_pct=int(stored.get("calibrated_pct", FULL_CALIBRATION)),
        tripped_until=None if tripped_until is None else parse_timestamp(tripped_until),
        last_trip_at=None if last_trip_at is None else parse_timestamp(last_trip_at),
    )


def _set_breaker(conn: sqlite3.Connection, **values: str) -> None:
    """Write the breaker's state, inside the caller's transaction."""
    for key, value in values.items():
        conn.execute(_STATE_SET, {"key": key, "value": value})


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
