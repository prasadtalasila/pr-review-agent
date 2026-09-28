"""The admission predicate, and what a claim may never reach across.

`admit` runs inside the claim transaction, which is what makes a budget
reservation atomic with the claim; a refused candidate is skipped rather
than ending the sweep, and no sweep touches another repository's rows.
"""

from dataclasses import replace
from datetime import timedelta

from queue_harness import NOON, REPO, mention, opened

from pr_review_agent.queue import DEFAULT_LEASE, QueueStatus, ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import CommentSource, TriggerKind

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
        queue = ReviewQueue(store, repo=REPO)
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


def test_release_unattempted_hands_the_row_back_without_the_attempt(queue):
    """For work that reached no engine: it drained nothing to measure."""
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert claim.attempts == 1

    assert queue.release_unattempted(claim) is True

    again = queue.claim(now=NOON, owner="w1")
    assert again.attempts == 1


def test_release_unattempted_never_abandons_a_row(queue):
    """Three failed posts of a paid review must not exhaust the bound."""
    queue.enqueue(opened(), now=NOON)
    for _ in range(5):
        claim = queue.claim(now=NOON, owner="w1")
        assert claim is not None
        queue.release_unattempted(claim)
    assert queue.status(opened().dedupe_key) is QueueStatus.PENDING


def test_release_unattempted_is_owner_guarded(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    stale = replace(claim, owner="w2")
    assert queue.release_unattempted(stale) is False


def test_holds_is_true_while_the_lease_lives(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert queue.holds(claim, now=NOON) is True


def test_holds_is_false_once_the_lease_lapses(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert queue.holds(claim, now=NOON + DEFAULT_LEASE + timedelta(seconds=1)) is False


def test_holds_is_false_for_another_owner(queue):
    """Asked before a write that cannot be taken back."""
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    assert queue.holds(replace(claim, owner="w2"), now=NOON) is False


def test_holds_is_false_once_the_row_is_finished(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w1")
    queue.complete(claim)
    assert queue.holds(claim, now=NOON) is False


OTHER_REPO = "someone-else/private-repo"


def elsewhere(pr=7):
    """A trigger from the repository this daemon holds no token for."""
    return replace(
        opened(pr=pr),
        repo=OTHER_REPO,
        dedupe_key=f"pr_opened:{OTHER_REPO}:{pr}:abc123",
    )


def test_a_claim_never_crosses_repositories(store):
    """The trust boundary the per-process split exists to draw.

    Several daemons share one store so they can share one budget, and the
    queue table is shared along with it. Each daemon holds only its own
    repository's token, so a trigger offered to the wrong process is a
    review attempted with credentials for somewhere else.
    """
    ReviewQueue(store, repo=OTHER_REPO).enqueue(elsewhere(), now=NOON)
    assert ReviewQueue(store, repo=REPO).claim(now=NOON, owner="w1") is None


def test_the_other_daemon_still_claims_its_own(store):
    """The complement: scoping must not strand the work it belongs to."""
    ReviewQueue(store, repo=OTHER_REPO).enqueue(elsewhere(), now=NOON)
    claim = ReviewQueue(store, repo=OTHER_REPO).claim(now=NOON, owner="w2")
    assert claim is not None
    assert claim.trigger.repo == OTHER_REPO


def test_one_repos_sweep_does_not_abandon_anothers_rows(store):
    """``claim`` sweeps exhausted rows before offering any; that is scoped too."""
    other = ReviewQueue(store, repo=OTHER_REPO, max_attempts=1)
    other.enqueue(elsewhere(), now=NOON)
    attempt = other.claim(now=NOON, owner="w1")
    assert attempt is not None
    other.release(attempt)

    # Repo A drains its own (empty) queue, running the sweep over the store.
    assert ReviewQueue(store, repo=REPO).claim(now=NOON, owner="w2") is None

    with store.transaction() as conn:
        status = conn.execute(
            "SELECT status FROM queue WHERE dedupe_key = ?",
            (elsewhere().dedupe_key,),
        ).fetchone()[0]
    assert status == str(QueueStatus.PENDING)


# -- folding: one review answers everything that was already waiting ------


def test_folding_closes_the_mentions_the_review_answered(queue):
    """Three people asking about one pull request asked one question.

    The agent keeps one comment per pull request and edits it in place, so
    reviewing each mention separately would pay three times to overwrite the
    same comment twice.
    """
    queue.enqueue(mention(comment_id=1), now=NOON)
    queue.enqueue(mention(comment_id=2), now=NOON)
    queue.enqueue(mention(comment_id=3), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None

    folded = queue.fold(claim, before=NOON + timedelta(seconds=1), head_sha="abc123")

    assert folded == 2
    assert queue.status(mention(comment_id=2).dedupe_key) is QueueStatus.DONE


def test_folding_leaves_a_trigger_enqueued_during_the_review(queue):
    """It may be asking about something the review never read."""
    queue.enqueue(mention(comment_id=1), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None
    started = NOON + timedelta(seconds=1)
    queue.enqueue(mention(comment_id=2), now=started + timedelta(minutes=4))

    assert queue.fold(claim, before=started, head_sha="abc123") == 0


def test_folding_leaves_a_trigger_that_named_another_commit(queue):
    """A request about a commit this review did not read is not answered by it."""
    queue.enqueue(mention(comment_id=1), now=NOON)
    queue.enqueue(opened(head_sha="something-else"), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None

    folded = queue.fold(claim, before=NOON + timedelta(seconds=1), head_sha="abc123")

    assert folded == 0
    assert queue.status(opened(head_sha="something-else").dedupe_key) is (
        QueueStatus.PENDING
    )


def test_folding_is_scoped_to_one_pull_request(queue):
    queue.enqueue(mention(pr=7, comment_id=1), now=NOON)
    queue.enqueue(mention(pr=8, comment_id=2), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None

    assert queue.fold(claim, before=NOON + timedelta(seconds=1), head_sha="x") == 0
