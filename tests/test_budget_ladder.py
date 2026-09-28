"""The ladder: what is still admitted as the window fills.

Degrading to mentions only, and scoping a heavy contributor's spend, are
both refusals that must not block the queue behind them -- a refused
candidate keeps its attempts and stays pending, and the next trigger is
still offered.
"""

from datetime import timedelta

from budget_harness import NOON, REPO, admit_all, budget, burn_to, mention, opened

from pr_review_agent.budget import Governor, Mode, StopReason, Usage, UsageConfidence
from pr_review_agent.queue import QueueStatus, ReviewQueue
from pr_review_agent.triggers.models import TriggerKind

# -- the ladder ---------------------------------------------------------


def test_the_ladder_switches_at_eighty_five_percent(store):
    """Below the rung a pull request is admitted; at it, only a mention is."""
    governor = Governor(store, budget(max_run_tokens=100))
    daily = budget().daily_limit
    queue = ReviewQueue(store, repo=REPO)

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
    queue = ReviewQueue(store, repo=REPO)
    burn_to(governor, queue, Mode.MENTION_ONLY)

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
    queue = ReviewQueue(store, repo=REPO)
    burn_to(governor, queue, Mode.MENTION_ONLY)

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
    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
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

    queue = ReviewQueue(store, repo=REPO)
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

    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(pr=1), now=NOON)
    assert queue.claim(now=NOON, owner="w") is not None


# -- the rung a claim was admitted under ---------------------------------


def test_the_admitted_rung_is_readable_from_the_claim(store):
    """The worker needs the rung to build a ReviewRequest, and Claim has none."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert governor.admitted_mode(claim) is Mode.FULL


def test_a_mention_admitted_under_the_rung_reports_it(store):
    governor = Governor(store, budget(max_run_tokens=100))
    queue = ReviewQueue(store, repo=REPO)
    burn_to(governor, queue, Mode.MENTION_ONLY)
    queue.enqueue(mention(pr=901, comment_id=5), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert governor.admitted_mode(claim) is Mode.MENTION_ONLY


def test_a_settled_claim_has_no_admitted_rung(store):
    """Settled is not unsettled: the reservation this asks about is gone."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
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
    queue = ReviewQueue(store, repo=REPO)
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
    assert claim is not None
    assert governor.admitted_mode(replace(claim, owner="somebody-else")) is None
