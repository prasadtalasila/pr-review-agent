"""ReviewQueue: dedupe, the per-pull-request lease, and the retry bound."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.queue import DEFAULT_LEASE, QueueStatus, ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import CommentSource, Trigger, TriggerKind

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
REPO = "INTO-CPS-Association/DTaaS"


def opened(pr=7, head_sha="abc123"):
    return Trigger(
        kind=TriggerKind.PR_OPENED,
        repo=REPO,
        pr_number=pr,
        head_sha=head_sha,
        actor_id=114395272,
        dedupe_key=f"pr_opened:{REPO}:{pr}:{head_sha}",
    )


def mention(pr=7, comment_id=99):
    return Trigger(
        kind=TriggerKind.MENTION,
        repo=REPO,
        pr_number=pr,
        head_sha=None,
        actor_id=114395272,
        dedupe_key=f"mention:{REPO}:{pr}:{comment_id}",
        comment_id=comment_id,
        comment_source=CommentSource.ISSUE,
    )


@pytest.fixture(name="queue")
def queue_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield ReviewQueue(store)


def test_a_trigger_is_enqueued_once(queue):
    assert queue.enqueue(opened(), now=NOON) is True
    assert queue.enqueue(opened(), now=NOON) is False


def test_a_new_head_is_new_work(queue):
    # The pull-request dedupe key carries head_sha for exactly this reason.
    assert queue.enqueue(opened(head_sha="aaa"), now=NOON) is True
    assert queue.enqueue(opened(head_sha="bbb"), now=NOON) is True


def test_an_empty_queue_yields_no_claim(queue):
    assert queue.claim(now=NOON, owner="w1") is None


def test_a_claim_carries_the_trigger(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim is not None
    assert (claim.trigger.kind, claim.trigger.repo, claim.trigger.pr_number) == (
        TriggerKind.PR_OPENED,
        REPO,
        7,
    )
    assert claim.trigger.head_sha == "abc123"
    assert claim.attempts == 1
    assert claim.leased_until == NOON + DEFAULT_LEASE


def test_a_mention_claim_has_no_head_sha(queue):
    # The comment payload does not name one; the worker resolves it.
    queue.enqueue(mention(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim is not None and claim.trigger.head_sha is None


def test_claiming_marks_the_row(queue):
    queue.enqueue(opened(), now=NOON)
    queue.claim(now=NOON, owner="w1")
    assert queue.status(opened().dedupe_key) is QueueStatus.CLAIMED


def test_an_unknown_key_has_no_status(queue):
    assert queue.status("mention:o/r:1:1") is None


def test_one_pull_request_is_not_reviewed_twice_at_once(queue):
    # A maintainer's @claude arriving mid-review must wait, not race it.
    queue.enqueue(opened(pr=7), now=NOON)
    queue.enqueue(mention(pr=7), now=NOON)
    assert queue.claim(now=NOON, owner="w1") is not None
    assert queue.claim(now=NOON, owner="w2") is None


def test_a_different_pull_request_is_claimable_concurrently(queue):
    queue.enqueue(opened(pr=7), now=NOON)
    queue.enqueue(opened(pr=8), now=NOON)
    first = queue.claim(now=NOON, owner="w1")
    second = queue.claim(now=NOON, owner="w2")
    assert first is not None and second is not None
    assert {first.trigger.pr_number, second.trigger.pr_number} == {7, 8}


def test_completing_frees_the_pull_request(queue):
    queue.enqueue(opened(pr=7), now=NOON)
    queue.enqueue(mention(pr=7), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim is not None
    assert queue.complete(claim) is True
    assert queue.status(claim.trigger.dedupe_key) is QueueStatus.DONE
    assert queue.claim(now=NOON, owner="w2") is not None


def test_a_completed_trigger_is_never_re_offered(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim is not None
    queue.complete(claim)
    assert queue.claim(now=NOON + 2 * DEFAULT_LEASE, owner="w2") is None


def test_releasing_hands_the_work_back_immediately(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim is not None
    assert queue.release(claim) is True
    again = queue.claim(now=NOON, owner="w2")
    assert again is not None and again.attempts == 2


def test_an_expired_lease_is_reclaimed(queue):
    # The crashed-worker path: no heartbeat, just an expiry.
    queue.enqueue(opened(), now=NOON)
    queue.claim(now=NOON, owner="crashed")
    later = NOON + DEFAULT_LEASE + timedelta(seconds=1)
    reclaimed = queue.claim(now=later, owner="w2")
    assert reclaimed is not None and reclaimed.owner == "w2"


def test_a_live_lease_is_not_stolen(queue):
    queue.enqueue(opened(), now=NOON)
    queue.claim(now=NOON, owner="w1")
    assert (
        queue.claim(now=NOON + DEFAULT_LEASE - timedelta(seconds=1), owner="w2") is None
    )


def test_a_lapsed_worker_cannot_finish_the_new_workers_row(queue):
    queue.enqueue(opened(), now=NOON)
    stale = queue.claim(now=NOON, owner="w1")
    assert stale is not None
    later = NOON + DEFAULT_LEASE + timedelta(seconds=1)
    assert queue.claim(now=later, owner="w2") is not None
    assert queue.complete(stale) is False
    assert queue.status(stale.trigger.dedupe_key) is QueueStatus.CLAIMED


def test_retries_are_bounded(tmp_path):
    # Without the bound, a trigger that crashes its worker every time is
    # re-reviewed forever, spending the allowance on each attempt.
    with SqliteStore(tmp_path / "state.db") as store:
        queue = ReviewQueue(store, max_attempts=2)
        queue.enqueue(opened(), now=NOON)
        moment = NOON
        for _ in range(2):
            assert queue.claim(now=moment, owner="crashes") is not None
            moment += DEFAULT_LEASE + timedelta(seconds=1)
        assert queue.claim(now=moment, owner="w2") is None
        assert queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


def test_a_released_trigger_is_abandoned_once_attempts_run_out(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        queue = ReviewQueue(store, max_attempts=1)
        queue.enqueue(opened(), now=NOON)
        claim = queue.claim(now=NOON, owner="w1")
        assert claim is not None and queue.release(claim) is True
        assert queue.claim(now=NOON, owner="w2") is None
        assert queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


def test_the_oldest_trigger_is_claimed_first(queue):
    queue.enqueue(opened(pr=8), now=NOON)
    queue.enqueue(opened(pr=7), now=NOON + timedelta(minutes=1))
    claim = queue.claim(now=NOON + timedelta(minutes=2), owner="w1")
    assert claim is not None and claim.trigger.pr_number == 8


def test_the_queue_survives_a_restart(tmp_path):
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        ReviewQueue(store).enqueue(opened(), now=NOON)
    with SqliteStore(path) as store:
        queue = ReviewQueue(store)
        assert queue.enqueue(opened(), now=NOON) is False
        assert queue.claim(now=NOON, owner="w1") is not None


def test_a_second_connection_cannot_double_lease(tmp_path):
    # The claim is a read followed by a write; BEGIN IMMEDIATE is what makes
    # the pair atomic against another writer on the same file.
    path = tmp_path / "state.db"
    with SqliteStore(path) as one, SqliteStore(path) as two:
        ReviewQueue(one).enqueue(opened(), now=NOON)
        assert ReviewQueue(one).claim(now=NOON, owner="w1") is not None
        assert ReviewQueue(two).claim(now=NOON, owner="w2") is None


def test_a_naive_timestamp_is_rejected(queue):
    with pytest.raises(ValueError):
        queue.enqueue(opened(), now=datetime(2026, 9, 17, 12, 0))


# -- the admit hook (the budget governor's seam) -------------------------


def test_admit_runs_inside_the_claim_transaction(tmp_path):
    """What it writes commits with the lease, which is the whole invariant."""
    seen = {}

    def admit(conn, claim, now):
        conn.execute("CREATE TABLE IF NOT EXISTS probe (key TEXT)")
        conn.execute("INSERT INTO probe VALUES (?)", (claim.trigger.dedupe_key,))
        seen["in_transaction"] = conn.in_transaction
        seen["now"] = now
        return True

    with SqliteStore(tmp_path / "state.db") as store:
        queue = ReviewQueue(store)
        queue.enqueue(opened(), now=NOON)
        claim = queue.claim(now=NOON, owner="w", admit=admit)
        assert claim is not None
        assert seen["in_transaction"] is True
        assert seen["now"] == NOON

        with store.transaction() as conn:
            assert conn.execute("SELECT count(*) FROM probe").fetchone()[0] == 1


def test_a_refused_candidate_is_skipped_not_final(queue):
    """The head-of-line rule: refusing one row must still offer the next."""
    queue.enqueue(opened(pr=1), now=NOON)
    queue.enqueue(mention(pr=2), now=NOON + timedelta(seconds=1))

    claim = queue.claim(
        now=NOON,
        owner="w",
        admit=lambda _conn, c, _now: c.trigger.kind is TriggerKind.MENTION,
    )
    assert claim is not None
    assert claim.trigger.pr_number == 2


def test_refusing_everything_claims_nothing_and_costs_no_attempt(queue):
    queue.enqueue(opened(), now=NOON)

    assert queue.claim(now=NOON, owner="w", admit=lambda *_: False) is None
    assert queue.status(opened().dedupe_key) is QueueStatus.PENDING

    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None
    assert claim.attempts == 1  # the refusal did not count against the bound


def test_abandoning_gives_up_permanently(queue):
    # A deterministic failure -- an oversized pull request -- must not be
    # retried, and must not be recorded as reviewed either.
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim is not None
    assert queue.abandon(claim) is True
    assert queue.status(claim.trigger.dedupe_key) is QueueStatus.ABANDONED
    assert queue.claim(now=NOON + 2 * DEFAULT_LEASE, owner="w2") is None


def test_abandoning_frees_the_pull_request(queue):
    queue.enqueue(opened(pr=7), now=NOON)
    queue.enqueue(mention(pr=7), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim is not None
    queue.abandon(claim)
    assert queue.claim(now=NOON, owner="w2") is not None


def test_a_lapsed_worker_cannot_abandon_the_new_workers_row(queue):
    queue.enqueue(opened(), now=NOON)
    stale = queue.claim(now=NOON, owner="w1")
    assert stale is not None
    later = NOON + DEFAULT_LEASE + timedelta(seconds=1)
    assert queue.claim(now=later, owner="w2") is not None
    assert queue.abandon(stale) is False
    assert queue.status(stale.trigger.dedupe_key) is QueueStatus.CLAIMED


# -- the fields the publisher reacts on ----------------------------------


def test_a_claim_restores_the_comment_it_came_from(queue):
    queue.enqueue(mention(comment_id=4321), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim.trigger.comment_id == 4321
    assert claim.trigger.comment_source is CommentSource.ISSUE


def test_a_claim_restores_a_review_comment_source(queue):
    trigger = replace(mention(), comment_source=CommentSource.REVIEW)
    queue.enqueue(trigger, now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim.trigger.comment_source is CommentSource.REVIEW


def test_a_pull_request_claim_names_no_comment(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim.trigger.comment_id is None
    assert claim.trigger.comment_source is None
