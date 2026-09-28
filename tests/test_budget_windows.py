"""The rolling windows, the reservation they hold, and whose limits bound them.

Reserve-then-settle is the whole protection: a reservation counts in full
until it is settled, a crashed run stays charged past its lease, and the
settle that closes it is guarded on the owner. A complier measures all of
that against the authority's published limits rather than its own.
"""

from datetime import timedelta

from budget_harness import (
    NOON,
    REPO,
    SESSION_TOKENS,
    WEEKLY_TOKENS,
    admit_all,
    budget,
    opened,
)

from pr_review_agent.budget import (
    DAILY,
    SESSION,
    WEEKLY,
    Governor,
    Mode,
    StopReason,
    Usage,
    UsageConfidence,
)
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import BudgetPolicy

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
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(pr=1), now=NOON)
    queue.claim(now=NOON, owner="w", admit=governor.admit)

    spent = 10_000 // 7 - governor.headroom(NOON).remaining
    assert spent == 1_000  # the reservation, not the (unknown) usage


def test_settle_releases_the_unused_remainder(store):
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(pr=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None

    assert governor.preflight(claim, 0, NOON) is not None

    with store.transaction() as conn:
        row = conn.execute("SELECT stop_reason, used_tokens FROM ledger").fetchone()
    assert row == (str(StopReason.REFUSED), 0)


def test_settle_is_guarded_on_the_owner(store):
    """A worker whose lease lapsed cannot settle a newer worker's row."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(pr=1), now=NOON)
    queue.claim(now=NOON, owner="w", admit=governor.admit)

    long_after_the_lease = NOON + timedelta(hours=2)
    before = governor.headroom(NOON).remaining
    assert governor.headroom(long_after_the_lease).remaining == before


def test_re_admitting_a_trigger_closes_the_reservation_it_left_open(store):
    """Issue #70: one open reservation per trigger, held by the schema.

    ``daemon.supervise`` restarts a crashed worker under the same ``owner``,
    so a retry reserved under the same ``(dedupe_key, owner)`` the crash
    left open. Two rows then matched one settle, which updated both and
    reported no single reservation -- and the worker discarded a review it
    had already paid for. ``admit`` closes the predecessor instead.
    """
    governor = Governor(
        store, budget(max_run_tokens=100, min_review_interval_seconds=0)
    )
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(pr=1), now=NOON)
    assert queue.claim(now=NOON, owner="w", admit=governor.admit) is not None

    after_the_lease = NOON + timedelta(hours=2)
    retry = queue.claim(now=after_the_lease, owner="w", admit=governor.admit)
    assert retry is not None

    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT stop_reason, reserved_tokens, used_tokens, usage_confidence "
            "FROM ledger ORDER BY id"
        ).fetchall()
    # The crashed attempt is charged the whole of what it reserved: what it
    # actually spent is unknowable, and for a spending control the
    # pessimistic answer is the safe one. The retry's row is the only open
    # one, so its settle is unambiguous.
    assert rows == [
        (str(StopReason.LOST), 100, 100, str(UsageConfidence.UNAVAILABLE)),
        (None, 100, None, None),
    ]
    assert governor.settle(
        retry,
        Usage(40, UsageConfidence.EXACT, engine="claude"),
        now=after_the_lease,
        stop_reason=StopReason.COMPLETED,
    )


def test_closing_a_lost_reservation_hands_back_no_allowance(store):
    """Settling it as ``lost`` is bookkeeping, not an amnesty.

    The windows counted the open reservation in full while it sat there and
    they count it in full afterwards. All that changes is that the charge is
    now a row saying why, which an operator can group and count.
    """
    governor = Governor(
        store, budget(max_run_tokens=100, min_review_interval_seconds=0)
    )
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(pr=1), now=NOON)
    assert queue.claim(now=NOON, owner="w", admit=governor.admit) is not None
    charged = governor.headroom(NOON).remaining

    after_the_lease = NOON + timedelta(hours=2)
    assert queue.claim(now=after_the_lease, owner="w", admit=governor.admit)

    # One reservation closed, one opened: the remainder moves by exactly the
    # new one, never by the old one being handed back.
    assert governor.headroom(after_the_lease).remaining == charged - 100


# -- the shared budget policy -------------------------------------------


def publish(store, config, repo=REPO, now=NOON):
    """Stand in for an authority daemon publishing its pool arithmetic."""
    store.publish_budget_policy(BudgetPolicy(repo, config.shared()), now=now)


def test_a_complier_measures_against_the_authoritys_limits(store):
    """One store is one pool, so one file governs it -- not each daemon's own.

    Without this every process would police the shared allowance using its
    own numbers, and two files that disagree do not split the pool: the most
    permissive keeps admitting after the others have correctly stopped.
    """
    authority = budget(weekly_tokens=100_000)
    publish(store, authority)
    governor = Governor(store, budget(), comply=True)

    admitted = admit_all(store, governor, [opened(pr=n) for n in range(60)])

    assert len(admitted) == authority.daily_limit // 1_000
    # The local file would have allowed far fewer, which is what makes this
    # test able to tell the two apart at all.
    assert authority.daily_limit > budget().daily_limit


def test_the_kill_switch_stays_local_to_one_repository(store):
    """``enabled`` is the emergency brake, so it is never adopted.

    A brake that could only be pulled fleet-wide could not stop one
    misbehaving repository without stopping every other one with it.
    """
    publish(store, budget())

    governor = Governor(store, budget(enabled=False), comply=True)

    assert admit_all(store, governor, [opened(pr=1)]) == []


def test_a_republished_policy_binds_the_next_claim(store):
    """An authority's SIGHUP reaches a running complier with no signal to it.

    The policy is read inside the transaction the reservation is written in,
    so the next claim simply measures against the new numbers.
    """
    publish(store, budget(weekly_tokens=100_000))
    governor = Governor(store, budget(), comply=True)
    assert admit_all(store, governor, [opened(pr=1)]) != []

    publish(store, budget(weekly_tokens=7_000))

    assert admit_all(store, governor, [opened(pr=2)]) == []


def test_a_governor_that_does_not_comply_ignores_the_policy(store):
    """The escape hatch: ``comply: false`` governs with this file alone."""
    publish(store, budget(weekly_tokens=100_000))

    governor = Governor(store, budget())

    admitted = admit_all(store, governor, [opened(pr=n) for n in range(60)])
    assert len(admitted) == budget().daily_limit // 1_000
