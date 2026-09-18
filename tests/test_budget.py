"""Governor: window arithmetic, the ladder, and reserve-then-settle.

The spend bounds are the point of this file. CLAUDE.md §5 requires a change
that widens what may be spent to add a test pinning the new bound, and these
are those bounds.
"""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.budget import (
    DAILY,
    DEFAULT_TOKENS_PER_LINE,
    MIN_FIT_SAMPLES,
    SESSION,
    WEEKLY,
    Governor,
    Mode,
    StopReason,
    Usage,
    UsageConfidence,
)
from pr_review_agent.config import BudgetConfig
from pr_review_agent.queue import QueueStatus, ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import Trigger, TriggerKind

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
REPO = "INTO-CPS-Association/DTaaS"

# 1000-token runs against a 10 000-token weekly share, so a percentage of the
# tightest window is a whole number of runs and the ladder lands on exact
# boundaries rather than near them.
WEEKLY_TOKENS = 25_000  # x 40% share = 10 000
SESSION_TOKENS = 25_000


def budget(**overrides):
    base = {
        "session_tokens": SESSION_TOKENS,
        "weekly_tokens": WEEKLY_TOKENS,
        "max_run_tokens": 1_000,
        "reviewer_share_pct": 40,
    }
    return BudgetConfig(**{**base, **overrides})


def opened(pr=7, head_sha="abc123", actor_id=114395272):
    return Trigger(
        kind=TriggerKind.PR_OPENED,
        repo=REPO,
        pr_number=pr,
        head_sha=head_sha,
        actor_id=actor_id,
        dedupe_key=f"pr_opened:{REPO}:{pr}:{head_sha}",
    )


def mention(pr=7, comment_id=99, actor_id=114395272):
    return Trigger(
        kind=TriggerKind.MENTION,
        repo=REPO,
        pr_number=pr,
        head_sha=None,
        actor_id=actor_id,
        dedupe_key=f"mention:{REPO}:{pr}:{comment_id}",
    )


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield store


def admit_all(store, governor, triggers, *, now=NOON):
    """Enqueue and claim each trigger through the governor; return the claims."""
    queue = ReviewQueue(store)
    claims = []
    for index, trigger in enumerate(triggers):
        queue.enqueue(trigger, now=now + timedelta(seconds=index))
    while (claim := queue.claim(now=now, owner="w", admit=governor.admit)) is not None:
        claims.append(claim)
        queue.complete(claim)
    return claims


# -- windows ------------------------------------------------------------


def test_share_caps_the_agent_below_the_plan():
    """The agent's ceiling is its share of the plan, never the whole plan."""
    assert budget().weekly_limit == WEEKLY_TOKENS * 40 // 100
    assert budget().session_limit == SESSION_TOKENS * 40 // 100
    assert budget().weekly_limit < WEEKLY_TOKENS


def test_daily_is_a_seventh_of_the_weekly_share():
    assert budget().daily_limit == 10_000 // 7


def test_daily_pacing_refuses_what_the_week_alone_would_allow(store):
    """The weekly window has room; the day does not. The tightest one wins."""
    governor = Governor(store, budget())
    admitted = admit_all(store, governor, [opened(pr=n) for n in range(20)])

    daily = budget().daily_limit
    assert len(admitted) == daily // 1_000
    assert governor.headroom(NOON).tightest == "daily"
    # The week is nowhere near spent -- the day is what stopped it.
    assert daily < 10_000


def test_an_old_run_falls_out_of_the_rolling_window(store):
    """A window is trailing: yesterday's spend does not bind today."""
    governor = Governor(store, budget())
    admit_all(store, governor, [opened(pr=1)], now=NOON - DAILY - timedelta(minutes=1))
    before = governor.headroom(NOON - DAILY - timedelta(minutes=1))
    assert before.remaining < governor.headroom(NOON).remaining


def test_the_session_window_can_be_the_tightest(store):
    """Five hours is shorter than a day, so it binds first on a burst."""
    governor = Governor(store, budget(session_tokens=2_500))  # 1 000 share
    admitted = admit_all(store, governor, [opened(pr=n) for n in range(5)])
    assert len(admitted) == 1
    assert governor.headroom(NOON).tightest == "session"
    assert SESSION < DAILY < WEEKLY


# -- reserve then settle ------------------------------------------------


def test_an_unsettled_reservation_counts_in_full(store):
    """The whole concurrency guarantee: a live hold is already spent."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    queue.claim(now=NOON, owner="w", admit=governor.admit)

    spent = 10_000 // 7 - governor.headroom(NOON).remaining
    assert spent == 1_000  # the reservation, not the (unknown) usage


def test_settle_releases_the_unused_remainder(store):
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    held = governor.headroom(NOON).remaining

    assert governor.settle(
        claim,
        Usage(250, UsageConfidence.EXACT, engine="claude_sdk", model="claude-opus-5"),
        now=NOON,
        stop_reason=StopReason.COMPLETED,
    )
    assert governor.headroom(NOON).remaining == held + 750


def test_settle_records_what_a_posted_comment_is_traced_to(store):
    """ROADMAP: engine, model, mode, usage and confidence on every row."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    governor.settle(
        claim,
        Usage(
            250, UsageConfidence.ESTIMATED, engine="claude_cli", model="claude-opus-5"
        ),
        now=NOON,
        stop_reason=StopReason.COMPLETED,
    )
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT engine, model, mode, used_tokens, usage_confidence, actor_id "
            "FROM ledger"
        ).fetchone()
    assert row == (
        "claude_cli",
        "claude-opus-5",
        str(Mode.FULL),
        250,
        str(UsageConfidence.ESTIMATED),
        114395272,
    )


def test_settle_records_why_the_run_stopped(store):
    """The reason is a column, not something inferred from the confidence."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None

    assert governor.settle(
        claim,
        Usage(1_000, UsageConfidence.UNAVAILABLE, engine="claude"),
        now=NOON,
        stop_reason=StopReason.TIMEOUT,
    )

    with store.transaction() as conn:
        row = conn.execute(
            "SELECT stop_reason, usage_confidence FROM ledger"
        ).fetchone()
    assert row == (str(StopReason.TIMEOUT), str(UsageConfidence.UNAVAILABLE))


def test_a_refused_preflight_reads_as_refused_not_as_a_failure(store):
    """A free refusal spent nothing, and the ledger should not imply it did."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None

    assert governor.preflight(claim, 0, NOON) is False

    with store.transaction() as conn:
        row = conn.execute("SELECT stop_reason, used_tokens FROM ledger").fetchone()
    assert row == (str(StopReason.REFUSED), 0)


def test_settle_is_guarded_on_the_owner(store):
    """A worker whose lease lapsed cannot settle a newer worker's row."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    stale = type(claim)(
        trigger=claim.trigger,
        attempts=claim.attempts,
        owner="somebody-else",
        leased_until=claim.leased_until,
    )
    assert not governor.settle(
        stale,
        Usage(1, UsageConfidence.EXACT),
        now=NOON,
        stop_reason=StopReason.COMPLETED,
    )


def test_a_crashed_run_stays_charged_past_its_lease(store):
    """Never settled, lease long gone -- the reservation still binds.

    BUDGET.md's "expires with the lease" is honoured as "not held forever",
    which the rolling window delivers, rather than as an amnesty: a crashed
    run has probably already spent, and it may be retried twice more.
    """
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    queue.claim(now=NOON, owner="w", admit=governor.admit)

    long_after_the_lease = NOON + timedelta(hours=2)
    before = governor.headroom(NOON).remaining
    assert governor.headroom(long_after_the_lease).remaining == before


# -- the ladder ---------------------------------------------------------


def test_the_ladder_switches_at_eighty_five_percent(store):
    """Below the rung a pull request is admitted; at it, only a mention is."""
    governor = Governor(store, budget(max_run_tokens=100))
    daily = budget().daily_limit
    queue = ReviewQueue(store)

    admitted = 0
    while governor.headroom(NOON).mode is Mode.FULL:
        queue.enqueue(opened(pr=admitted), now=NOON + timedelta(seconds=admitted))
        claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
        assert claim is not None
        queue.complete(claim)
        admitted += 1

    used = admitted * 100
    assert used / daily >= 0.85
    assert (used - 100) / daily < 0.85


def test_mention_only_admits_a_mention_and_refuses_a_pull_request(store):
    governor = Governor(store, budget(max_run_tokens=100))
    queue = ReviewQueue(store)
    _burn_to(governor, queue, Mode.MENTION_ONLY)

    queue.enqueue(opened(pr=900), now=NOON)
    queue.enqueue(mention(pr=901, comment_id=5), now=NOON + timedelta(seconds=1))
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert claim.trigger.kind is TriggerKind.MENTION


def test_a_refused_pull_request_does_not_block_a_mention_behind_it(store):
    """The head-of-line case: FIFO plus refusal must not deadlock the queue.

    The pull request is enqueued first, so a single-candidate claim would
    return it, be refused, and leave the maintainer's @claude unreachable
    until the window rolled -- days.
    """
    governor = Governor(store, budget(max_run_tokens=100))
    queue = ReviewQueue(store)
    _burn_to(governor, queue, Mode.MENTION_ONLY)

    queue.enqueue(opened(pr=900), now=NOON)
    queue.enqueue(mention(pr=901, comment_id=5), now=NOON + timedelta(seconds=1))

    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert claim.trigger.pr_number == 901


def test_a_refused_candidate_keeps_its_attempts_and_stays_pending(store):
    """A refusal is about the allowance, not the trigger.

    Burning an attempt would let three budget refusals abandon a perfectly
    good trigger for a reason that had nothing to do with it.
    """
    governor = Governor(store, budget(enabled=False))
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)

    for _ in range(5):
        assert queue.claim(now=NOON, owner="w", admit=governor.admit) is None

    key = opened(pr=1).dedupe_key
    assert queue.status(key) is QueueStatus.PENDING
    assert queue.claim(now=NOON, owner="w") is not None  # still claimable


def test_exhausted_refuses_a_mention_too(store):
    """Only an overrun reaches 100 %, which is exactly why the rung exists.

    Admission alone cannot get there: a run is refused once the remainder is
    below ``max_run_tokens``, so utilisation stalls just short. The window is
    breached only when a run *settles above what it reserved* -- an engine
    that overran -- and that is the case a hard stop has to cover.
    """
    governor = Governor(store, budget(max_run_tokens=100))
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    governor.settle(
        claim,
        Usage(budget().daily_limit, UsageConfidence.EXACT),
        now=NOON,
        stop_reason=StopReason.COMPLETED,
    )
    assert governor.headroom(NOON).mode is Mode.EXHAUSTED

    queue.enqueue(mention(pr=901, comment_id=5), now=NOON)
    assert queue.claim(now=NOON, owner="w", admit=governor.admit) is None


def test_a_run_that_does_not_fit_whole_is_refused(store):
    """No partial reservation: a truncated review is still a spend."""
    governor = Governor(store, budget(max_run_tokens=1_000))
    daily = budget().daily_limit
    admitted = admit_all(store, governor, [opened(pr=n) for n in range(20)])
    assert len(admitted) * 1_000 <= daily
    assert governor.headroom(NOON).remaining < 1_000


# -- the per-contributor window -----------------------------------------

# A second allowlisted account, so "this contributor is spent" can be told
# apart from "the agent is spent".
HEAVY, OTHER = 114395272, 8_675_309


def test_without_the_cap_nothing_is_scoped_to_a_contributor(store):
    """Unset is the default, and it admits exactly what it admits today."""
    governor = Governor(store, budget())
    triggers = [opened(pr=n, actor_id=HEAVY) for n in range(20)]
    admitted = admit_all(store, governor, triggers)
    assert len(admitted) == budget().daily_limit // 1_000
    assert governor.headroom(NOON).tightest == "daily"


def test_the_cap_refuses_a_second_run_by_the_same_contributor(store):
    """One contributor's share runs out while another's is untouched.

    1 % of the 10 000-token weekly share is 100 tokens: exactly one run.
    The daily window has room for fourteen, so only the contributor window
    can be what stops the second one.
    """
    governor = Governor(store, budget(max_run_tokens=100, per_contributor_pct=1))
    admitted = admit_all(
        store,
        governor,
        [
            opened(pr=1, actor_id=HEAVY),
            opened(pr=2, actor_id=HEAVY),
            opened(pr=3, actor_id=OTHER),
        ],
    )
    assert [claim.trigger.pr_number for claim in admitted] == [1, 3]


def test_the_refusal_names_the_contributor_window(store, caplog):
    governor = Governor(store, budget(max_run_tokens=100, per_contributor_pct=1))
    with caplog.at_level("WARNING"):
        admit_all(
            store,
            governor,
            [opened(pr=1, actor_id=HEAVY), opened(pr=2, actor_id=HEAVY)],
        )
    assert "contributor window" in caplog.text


def test_a_heavy_contributor_degrades_to_mention_only_first(store):
    """The ladder applies per contributor: auto-review stops, @claude does not.

    Nine 100-token runs against a 1 000-token contributor share is 90 %,
    past the rung but short of exhaustion.
    """
    governor = Governor(store, budget(max_run_tokens=100, per_contributor_pct=10))
    admit_all(store, governor, [opened(pr=n, actor_id=HEAVY) for n in range(9)])

    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=900, actor_id=HEAVY), now=NOON)
    queue.enqueue(
        mention(pr=901, comment_id=5, actor_id=HEAVY), now=NOON + timedelta(seconds=1)
    )
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert claim.trigger.kind is TriggerKind.MENTION


def test_one_spent_contributor_does_not_degrade_another(store):
    """The window is scoped to the actor being admitted, not to the agent."""
    governor = Governor(store, budget(max_run_tokens=100, per_contributor_pct=1))
    admit_all(store, governor, [opened(pr=1, actor_id=HEAVY)])

    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=2, actor_id=OTHER), now=NOON)
    assert queue.claim(now=NOON, owner="w", admit=governor.admit) is not None


def test_the_operator_readout_stays_global(store):
    """``headroom`` has no actor to scope to, so it reports the shared windows."""
    governor = Governor(store, budget(max_run_tokens=100, per_contributor_pct=1))
    admit_all(store, governor, [opened(pr=1, actor_id=HEAVY)])
    assert governor.headroom(NOON).tightest != "contributor"
    assert governor.headroom(NOON).mode is Mode.FULL


# -- the kill switch ----------------------------------------------------


def test_disabled_admits_nothing(store):
    governor = Governor(store, budget(enabled=False))
    assert not admit_all(store, governor, [opened(pr=1), mention(pr=2)])


def test_reload_takes_effect_without_a_restart(store):
    governor = Governor(store, budget(enabled=False))
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    assert queue.claim(now=NOON, owner="w", admit=governor.admit) is None

    governor.reload(budget(enabled=True))
    assert queue.claim(now=NOON, owner="w", admit=governor.admit) is not None


def test_no_admit_hook_leaves_the_queue_unguarded(store):
    """Pinning the known weakness of the seam, so it is not a surprise.

    ``admit`` is optional, so "nothing spends outside the governor" is a
    convention held by review rather than by the type system. The only
    caller that will ever claim is the worker the engine phase adds.
    """
    queue = ReviewQueue(store)
    queue.enqueue(opened(pr=1), now=NOON)
    assert queue.claim(now=NOON, owner="w") is not None


# -- the rung a claim was admitted under ---------------------------------


def test_the_admitted_rung_is_readable_from_the_claim(store):
    """The worker needs the rung to build a ReviewRequest, and Claim has none."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert governor.admitted_mode(claim) is Mode.FULL


def test_a_mention_admitted_under_the_rung_reports_it(store):
    governor = Governor(store, budget(max_run_tokens=100))
    queue = ReviewQueue(store)
    _burn_to(governor, queue, Mode.MENTION_ONLY)
    queue.enqueue(mention(pr=901, comment_id=5), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert governor.admitted_mode(claim) is Mode.MENTION_ONLY


def test_a_settled_claim_has_no_admitted_rung(store):
    """Settled is not unsettled: the reservation this asks about is gone."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    governor.settle(
        claim,
        Usage(10, UsageConfidence.EXACT),
        now=NOON,
        stop_reason=StopReason.COMPLETED,
    )
    assert governor.admitted_mode(claim) is None


def test_a_lapsed_workers_claim_has_no_admitted_rung(store):
    """Owner-guarded, exactly as settle is: the row belongs to somebody else."""
    from dataclasses import replace

    governor = Governor(store, budget())
    queue = ReviewQueue(store)
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert governor.admitted_mode(replace(claim, owner="somebody-else")) is None


def _burn_to(governor, queue, mode):
    """Admit runs until the ladder reaches ``mode``."""
    index = 0
    while governor.headroom(NOON).mode is not mode:
        queue.enqueue(opened(pr=index), now=NOON + timedelta(seconds=index))
        claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
        assert claim is not None, f"never reached {mode}"
        queue.complete(claim)
        index += 1


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
    assert governor.preflight(claim, over, NOON) is False


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

    assert governor.preflight(claim, 10_000, NOON) is False
    assert governor.headroom(NOON).remaining == budget().daily_limit


def test_an_affordable_pull_request_keeps_its_reservation(store):
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    assert governor.preflight(claim, 1, NOON) is True
    assert governor.headroom(NOON).remaining < budget().daily_limit


def test_nothing_left_to_review_is_refused_for_free(store):
    """A lockfile-only pull request has nothing in it after exclusions."""
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    assert governor.preflight(claim, 0, NOON) is False
    assert governor.headroom(NOON).remaining == budget().daily_limit


def test_a_refusal_never_contributes_to_the_fit(store):
    """It reviewed no lines, so it says nothing about tokens per line."""
    governor = Governor(store, budget())
    (claim,) = admit_all(store, governor, [opened(pr=1)])
    governor.preflight(claim, 0, NOON)
    with store.transaction() as conn:
        assert conn.execute("SELECT reviewed_lines FROM ledger").fetchone() == (None,)
