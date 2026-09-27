"""The pacer: how often one pull request may be reviewed.

The windows in `test_budget.py` bound a total across everything the agent
does. These bound a *rate*, per pull request, and the two are independent:
every test here leaves the windows nowhere near spent, so a refusal can only
have come from the pacer.

CLAUDE.md §5 asks a change that widens what may be spent to pin the new
bound. These are that bound, and the deferral it is made of -- a paced
trigger waits, it is never dropped.
"""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.budget import Governor
from pr_review_agent.config import BudgetConfig
from pr_review_agent.pacing import CAP_WINDOW
from pr_review_agent.queue import QueueStatus, ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import Trigger, TriggerKind

NOON = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
REPO = "prasadtalasila/pr-review-agent"
INTERVAL = 900
MENTION_INTERVAL = 300


def budget(**overrides) -> BudgetConfig:
    """A budget with room to spare, so only the pacer can refuse."""
    base = {
        "session_tokens": 1_000_000,
        "weekly_tokens": 10_000_000,
        "max_run_tokens": 1_000,
        "min_review_interval_seconds": INTERVAL,
        "mention_min_review_interval_seconds": MENTION_INTERVAL,
    }
    return BudgetConfig(**{**base, **overrides})


def opened(pr=7, head_sha="abc123") -> Trigger:
    return Trigger(
        kind=TriggerKind.PR_OPENED,
        repo=REPO,
        pr_number=pr,
        head_sha=head_sha,
        actor_id=114395272,
        dedupe_key=f"pr_opened:{REPO}:{pr}:{head_sha}",
    )


def mention(pr=7, comment_id=99) -> Trigger:
    return Trigger(
        kind=TriggerKind.MENTION,
        repo=REPO,
        pr_number=pr,
        head_sha=None,
        actor_id=114395272,
        dedupe_key=f"mention:{REPO}:{pr}:{comment_id}",
    )


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield store


def claim_one(queue, governor, trigger, *, now):
    """Enqueue ``trigger`` and try to claim it; ``None`` if it was refused."""
    queue.enqueue(trigger, now=now)
    return queue.claim(now=now, owner="w", admit=governor.admit)


def review(queue, governor, trigger, *, now):
    """One whole admitted review, as the worker would leave it."""
    claim = claim_one(queue, governor, trigger, now=now)
    assert claim is not None
    queue.complete(claim)
    return claim


# -- the interval -------------------------------------------------------


def test_a_second_review_inside_the_interval_is_deferred(store):
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
    review(queue, governor, opened(), now=NOON)

    later = NOON + timedelta(seconds=MENTION_INTERVAL - 1)
    assert claim_one(queue, governor, mention(), now=later) is None


def test_a_deferred_trigger_waits_rather_than_being_dropped(store):
    """The row is still pending and has spent no attempt: it will be claimed.

    This is the whole difference between pacing and refusing. A dropped
    mention is a maintainer ignored; a deferred one is a maintainer answered
    late, against a head that has stopped moving.
    """
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
    review(queue, governor, opened(), now=NOON)
    deferred = mention()

    assert claim_one(queue, governor, deferred, now=NOON) is None

    assert queue.status(deferred.dedupe_key) is QueueStatus.PENDING
    claim = queue.claim(
        now=NOON + timedelta(seconds=MENTION_INTERVAL),
        owner="w",
        admit=governor.admit,
    )
    assert claim is not None
    assert claim.trigger.dedupe_key == deferred.dedupe_key
    assert claim.attempts == 1


def test_a_mention_waits_the_shorter_interval(store):
    """Both are paced; a person waiting is paced less than a repository event."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
    review(queue, governor, opened(), now=NOON)
    between = NOON + timedelta(seconds=MENTION_INTERVAL + 1)

    assert claim_one(queue, governor, mention(), now=between) is not None
    assert claim_one(queue, governor, opened(head_sha="def456"), now=between) is None


def test_the_interval_binds_one_pull_request_only(store):
    """A busy pull request must not hold the queue for every other one."""
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
    review(queue, governor, opened(pr=7), now=NOON)

    assert claim_one(queue, governor, opened(pr=8), now=NOON) is not None


def test_zero_disables_the_interval(store):
    """Spelled rather than approximated by a very small number."""
    governor = Governor(
        store,
        budget(min_review_interval_seconds=0, mention_min_review_interval_seconds=0),
    )
    queue = ReviewQueue(store, repo=REPO)
    review(queue, governor, opened(), now=NOON)

    assert claim_one(queue, governor, mention(), now=NOON) is not None


def test_a_failed_run_still_paces_the_next_one(store):
    """The ledger, not the runs table: a run that recorded nothing spent too.

    A review that reached an engine and then failed has a ledger row and no
    recorded findings. Pacing off the findings would let a pull request that
    crash-loops the engine be re-reviewed without limit, which is the case
    that empties a budget fastest.
    """
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)
    claim = claim_one(queue, governor, opened(), now=NOON)
    assert claim is not None
    queue.release(claim)  # reached an engine, produced nothing

    assert claim_one(queue, governor, mention(), now=NOON) is None


# -- the daily cap ------------------------------------------------------


def test_the_cap_refuses_past_its_count(store):
    governor = Governor(
        store,
        budget(
            max_reviews_per_pull_request=2,
            min_review_interval_seconds=0,
            mention_min_review_interval_seconds=0,
        ),
    )
    queue = ReviewQueue(store, repo=REPO)
    review(queue, governor, opened(), now=NOON)
    review(queue, governor, mention(comment_id=1), now=NOON)

    assert claim_one(queue, governor, mention(comment_id=2), now=NOON) is None


def test_the_cap_is_a_trailing_window_rather_than_a_total(store):
    """Yesterday's reviews do not bind today's: the cap rolls, like the windows."""
    governor = Governor(
        store,
        budget(
            max_reviews_per_pull_request=1,
            min_review_interval_seconds=0,
            mention_min_review_interval_seconds=0,
        ),
    )
    queue = ReviewQueue(store, repo=REPO)
    review(queue, governor, opened(), now=NOON)
    tomorrow = NOON + CAP_WINDOW + timedelta(minutes=1)

    assert claim_one(queue, governor, mention(), now=tomorrow) is not None


def test_no_cap_is_the_default(store):
    """Off by default, for the reason `per_contributor_pct` is."""
    assert budget().max_reviews_per_pull_request is None
    governor = Governor(
        store,
        budget(min_review_interval_seconds=0, mention_min_review_interval_seconds=0),
    )
    queue = ReviewQueue(store, repo=REPO)
    for index in range(20):
        review(queue, governor, mention(comment_id=index), now=NOON)


# -- an upgraded database -----------------------------------------------


def test_a_ledger_row_from_before_the_migration_reads_as_no_history(store):
    """`repo` and `pr_number` are NULL on every row written before migration 11.

    Treating a missing column as *recent* would defer every review on an
    upgraded database until the backfill nobody can do had happened.
    """
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO ledger (dedupe_key, owner, actor_id, mode, "
            "reserved_tokens, reserved_at) VALUES ('old', 'w', 1, 'full', 10, ?)",
            (NOON.isoformat(),),
        )
    governor = Governor(store, budget())
    queue = ReviewQueue(store, repo=REPO)

    assert claim_one(queue, governor, opened(), now=NOON) is not None
