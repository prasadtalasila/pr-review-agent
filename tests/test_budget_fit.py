"""What a review is predicted to cost, and the breaker that decays it.

The estimate starts at a documented constant, takes over from the ledger
once there are samples to fit, and is cut by a usage limit -- which is the
one signal that the plan's real ceiling is lower than the arithmetic here
believed.
"""

import itertools

from budget_harness import NOON, REPO, admit_all, budget, mention, opened

from pr_review_agent.breaker import (
    DECAY_FACTOR,
    MIN_CALIBRATION,
    RECOVERY_PERIOD,
    TRIP_HOLD,
)
from pr_review_agent.budget import (
    DEFAULT_TOKENS_PER_LINE,
    MIN_FIT_SAMPLES,
    Governor,
    StopReason,
    Usage,
    UsageConfidence,
)
from pr_review_agent.queue import QueueStatus, ReviewQueue

# -- the pre-flight estimate --------------------------------------------


def fit_rows(store, governor, count, *, tokens, lines, confidence=None):
    """Settle ``count`` runs, each spending ``tokens`` over ``lines`` lines."""
    claims = admit_all(store, governor, [opened(pr=n) for n in range(count)])
    assert len(claims) == count, "the window ran out before the sample did"
    for claim in claims:
        governor.settle(
            claim,
            Usage(
                tokens=tokens,
                confidence=confidence or UsageConfidence.EXACT,
                engine="fake",
            ),
            now=NOON,
            stop_reason=StopReason.COMPLETED,
            reviewed_lines=lines,
        )


def test_an_empty_ledger_estimates_at_the_documented_constant(store):
    """The cold start errs high, deliberately.

    A fresh database has nothing to fit against, and the first runs are when
    an over-estimate is cheapest to get wrong.
    """
    governor = Governor(store, budget())
    assert governor.estimate(100) == 100 * DEFAULT_TOKENS_PER_LINE


def test_an_empty_ledger_still_refuses_an_oversized_pull_request(store):
    """Issue #17: the cold start is guarded, not exempt."""
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    over = budget().max_run_tokens // DEFAULT_TOKENS_PER_LINE + 1
    assert governor.preflight(claim, over, NOON) is not None


def test_the_fit_takes_over_once_the_sample_exists(store):
    """Evidence replaces the guess outright -- there is no floor under it."""
    governor = Governor(store, budget(weekly_tokens=250_000, session_tokens=250_000))
    fit_rows(store, governor, MIN_FIT_SAMPLES, tokens=300, lines=100)
    assert governor.estimate(100) == 300
    assert governor.estimate(100) < 100 * DEFAULT_TOKENS_PER_LINE


def test_one_sample_short_keeps_the_constant(store):
    governor = Governor(store, budget(weekly_tokens=250_000, session_tokens=250_000))
    fit_rows(store, governor, MIN_FIT_SAMPLES - 1, tokens=300, lines=100)
    assert governor.estimate(100) == 100 * DEFAULT_TOKENS_PER_LINE


def test_rows_the_engine_could_not_measure_are_not_fitted(store):
    """`estimated` and `unavailable` describe a run nobody measured.

    Fitting a rate to them would turn the weaker guarantee ENGINE.md
    describes into a confidently wrong number.
    """
    governor = Governor(store, budget(weekly_tokens=250_000, session_tokens=250_000))
    fit_rows(
        store,
        governor,
        MIN_FIT_SAMPLES,
        tokens=300,
        lines=100,
        confidence=UsageConfidence.ESTIMATED,
    )
    assert governor.estimate(100) == 100 * DEFAULT_TOKENS_PER_LINE


def test_a_refused_run_hands_its_reservation_straight_back(store):
    """Issue #17's criterion, honoured as "released" rather than "never taken".

    The reservation is taken inside the claim, before the pull request's size
    is knowable. What must not happen is that a refusal leaves it charged.
    """
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    assert governor.headroom(NOON).remaining < budget().daily_limit

    assert governor.preflight(claim, 10_000, NOON) is not None
    assert governor.headroom(NOON).remaining == budget().daily_limit


def test_an_affordable_pull_request_keeps_its_reservation(store):
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    assert governor.preflight(claim, 1, NOON) is None
    assert governor.headroom(NOON).remaining < budget().daily_limit


def test_nothing_left_to_review_is_refused_for_free(store):
    """A lockfile-only pull request has nothing in it after exclusions."""
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    assert governor.preflight(claim, 0, NOON) is not None
    assert governor.headroom(NOON).remaining == budget().daily_limit


def test_each_refusal_names_the_setting_that_would_change_it(store):
    """Issue #78: a refusal a contributor cannot act on is a silence.

    The two arms say different things because the operator action differs:
    one is a cap to raise, the other is a pull request every path of which
    is configured out of review, where raising a cap changes nothing.
    """
    governor = Governor(store, budget(weekly_tokens=250_000, session_tokens=250_000))
    empty, expensive = admit_all(store, governor, [opened(pr=1), opened(pr=2)])

    nothing_left = governor.preflight(empty, 0, NOON)
    over_budget = governor.preflight(expensive, 10_000, NOON)

    assert nothing_left is not None
    assert "budget.excluded_paths" in nothing_left
    assert over_budget is not None
    assert "budget.max_run_tokens" in over_budget


def test_a_refusal_never_contributes_to_the_fit(store):
    """It reviewed no lines, so it says nothing about tokens per line."""
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    governor.preflight(claim, 0, NOON)
    with store.transaction() as conn:
        assert conn.execute("SELECT reviewed_lines FROM ledger").fetchone() == (None,)


# -- the circuit breaker ------------------------------------------------


def test_a_trip_refuses_every_claim(store):
    """The wall is the account's, so nothing may be admitted behind it."""
    governor = Governor(store, budget())
    governor.trip(NOON)

    assert admit_all(store, governor, [opened(pr=1), mention(comment_id=2)]) == []


def test_a_refused_claim_costs_no_attempt(store):
    """A trip is about the allowance, not the trigger: it must not abandon one."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(pr=1), now=NOON)
    governor.trip(NOON)

    for _ in range(5):
        assert queue.claim(now=NOON, owner="w", admit=governor.admit) is None

    assert queue.status(opened(pr=1).dedupe_key) is QueueStatus.PENDING
    assert queue.claim(now=NOON + TRIP_HOLD, owner="w", admit=governor.admit)


def test_admission_resumes_once_the_hold_expires(store):
    governor = Governor(store, budget())
    governor.trip(NOON)

    assert admit_all(store, governor, [opened(pr=1)], now=NOON + TRIP_HOLD) != []


def test_a_trip_decays_the_calibration(store):
    governor = Governor(store, budget())

    assert governor.trip(NOON) == int(100 * DECAY_FACTOR)


def test_the_calibration_bounds_every_window(store):
    """The effective limit is the configured one times what was learned."""
    governor = Governor(store, budget())
    before = governor.headroom(NOON).remaining
    governor.trip(NOON)

    after = governor.headroom(NOON).remaining
    assert after == before * int(100 * DECAY_FACTOR) // 100


def test_the_calibration_floors_above_zero(store):
    """Never zero: `_headroom` divides by the limit, so zero would raise."""
    governor = Governor(store, budget())
    at = NOON
    for _ in range(200):
        governor.trip(at)

    assert governor.breaker().calibrated_pct == MIN_CALIBRATION
    # Still arithmetic rather than a ZeroDivisionError -- and a window this
    # small cannot fit a run, so the answer is a refusal either way.
    later = at + TRIP_HOLD
    assert governor.headroom(later).remaining < budget().max_run_tokens
    assert admit_all(store, governor, [opened(pr=1)], now=later) == []


def test_repeated_trips_converge_downward(store):
    """#20's criterion: downward, not oscillation."""
    governor = Governor(store, budget())
    seen = [governor.trip(NOON + n * TRIP_HOLD) for n in range(10)]

    assert seen == sorted(seen, reverse=True)
    assert seen[-1] < seen[0]
    # Strictly downward while the wall keeps being hit, despite a full
    # recovery period passing between each trip.
    assert all(later < earlier for earlier, later in itertools.pairwise(seen))


def test_a_clean_window_recovers_a_point(store):
    governor = Governor(store, budget())
    governor.trip(NOON)
    breaker = governor.breaker()

    assert breaker.calibration(NOON) == int(100 * DECAY_FACTOR)
    assert breaker.calibration(NOON + RECOVERY_PERIOD) == int(100 * DECAY_FACTOR) + 1


def test_recovery_caps_at_the_configured_limit(store):
    """It climbs back to the operator's guess and stops -- never above it."""
    governor = Governor(store, budget())
    governor.trip(NOON)

    assert governor.breaker().calibration(NOON + 500 * RECOVERY_PERIOD) == 100


def test_the_calibration_survives_a_restart(store):
    """The whole point of persisting it: a daemon restart is not an amnesty."""
    Governor(store, budget()).trip(NOON)

    assert Governor(store, budget()).breaker().calibrated_pct == round(
        100 * DECAY_FACTOR
    )
