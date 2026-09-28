"""Acknowledge, record, publish: what happens to a review that was paid for.

A paid review is never abandoned for a failed post. It is stamped, retried
under its own queue row, and answered to every trigger that was waiting.
"""

import logging
from dataclasses import replace

import pytest
from worker_harness import (
    NOW,
    POSIX_ONLY,
    PR,
    REPO,
    GitHubDouble,
    OutcomeEngine,
    ledger_rows,
    mention,
    opened,
    posted_comment,
    publish_state,
    recorded_result,
)

from pr_review_agent.comments import AgentComments
from pr_review_agent.engine import FakeEngine
from pr_review_agent.engine.models import Outcome
from pr_review_agent.queue import QueueStatus
from pr_review_agent.runs import RecordedRun, publication_of

pytestmark = POSIX_ONLY


# -- the publisher's two call sites --------------------------------------
#
# The acknowledgement is immediate and the publication is late, and the
# order between them is what the 15 s criterion rests on.


async def test_the_trigger_is_acknowledged_before_anything_slow(wired):
    """A review takes minutes; the 👀 must not queue behind it."""
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.paths[0].endswith("/reactions")


async def test_a_mention_is_acknowledged_on_its_own_comment(wired):
    fixture = wired()
    fixture.queue.enqueue(mention(comment_id=4321), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.reactions[0].url.path == (
        "/repos/owner/name/issues/comments/4321/reactions"
    )


async def test_a_failed_acknowledgement_still_yields_a_review(wired, git_remote):
    """Losing a courtesy must not cost a reserved review."""
    github = GitHubDouble(git_remote.head_sha, write_status=500)
    fixture = wired(github=github)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.engine.requests  # the engine still ran
    (row,) = ledger_rows(fixture.store)
    assert row[3] == 1_000  # and it still settled what it spent


async def test_a_completed_review_is_recorded_then_published(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.comments[0].method == "POST"
    assert posted_comment(fixture.store, opened().dedupe_key) == 555
    # And the agent knows the comment is its own, so the next poll cycle
    # cannot read it back as somebody asking for a review (issue #108).
    assert AgentComments(fixture.store).ids_for(REPO) == frozenset({555})
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE


async def test_one_review_answers_the_mentions_that_were_waiting(wired):
    """Three mentions, one engine run, one comment, three closed rows.

    The pacer would defer the second and third; folding is what stops them
    being reviewed one interval apart afterwards, because by then the review
    they were waiting for has already been posted.
    """
    fixture = wired()
    for comment_id in (1, 2, 3):
        fixture.queue.enqueue(mention(comment_id=comment_id), now=NOW)

    assert await fixture.worker.run_once() is True

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    assert len(engine.requests) == 1
    posted = [r for r in fixture.github.comments if r.url.path.endswith("/comments")]
    assert len(posted) == 1
    assert [
        fixture.queue.status(mention(comment_id=c).dedupe_key) for c in (1, 2, 3)
    ] == [QueueStatus.DONE] * 3


async def test_a_superseded_head_is_posted_once_and_stamped(wired, git_remote):
    """The review ran against a head the pull request has since left.

    The head moves between the worker's read and the publisher's, which is
    the only way it can move: both read the same endpoint. The tokens were
    spent before that read, so the review is posted saying which commit it
    describes rather than discarded.

    The stamp is issue #68: an unstamped run stays "still owed a comment"
    for the lifetime of the database, and every claim that took the offer
    re-read the same moved head.
    """
    github_moving = GitHubDouble(
        git_remote.head_sha, head_moves_to="a-newer-commit-entirely"
    )
    fixture = wired(github=github_moving)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert len(fixture.github.comments) == 1
    assert fixture.runs.unpublished(opened().dedupe_key) is None
    # Done rather than retried: a push is not a trigger, so another attempt
    # would re-read the same stale sha and reserve allowance to do it.
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE


async def test_a_failed_publish_is_retried_without_a_second_review(wired, git_remote):
    """The money is already spent; a flaky write must not spend it again."""
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()
    # The review is done and is not re-offered; what is outstanding is the
    # posting, and it is a row of its own.
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE
    assert fixture.queue.status(_publish_key()) is QueueStatus.PENDING
    assert len(fixture.engine.requests) == 1

    github._write_status = 201  # GitHub recovers
    await fixture.worker.run_once()

    assert len(fixture.engine.requests) == 1  # the engine was not run again
    assert posted_comment(fixture.store, opened().dedupe_key) == 555
    assert fixture.queue.status(_publish_key()) is QueueStatus.DONE


def _publish_key(trigger=None):
    """The queue key of the publication item for ``trigger``'s recorded run."""
    trigger = opened() if trigger is None else trigger
    return publication_of(trigger, _run_named(trigger.dedupe_key)).dedupe_key


def _run_named(key):
    return RecordedRun(
        dedupe_key=key,
        repo=REPO,
        pr_number=PR,
        head_sha="abc123",
        outcome=Outcome.COMPLETED,
        findings=(),
        comment_id=None,
    )


async def test_a_republished_run_writes_no_ledger_row(wired, git_remote):
    """A publication item reserves nothing, so there is nothing to settle."""
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github)
    fixture.queue.enqueue(opened(), now=NOW)
    await fixture.worker.run_once()
    assert len(ledger_rows(fixture.store)) == 1

    github._write_status = 201
    await fixture.worker.run_once()

    assert len(ledger_rows(fixture.store)) == 1  # the item added none


async def test_failed_posts_never_abandon_a_paid_review(wired, git_remote):
    """The attempt bound caps what a poison trigger drains, not this.

    Three failed *posts* of a review that is already paid for would once
    have exhausted the bound and abandoned the row, leaving the findings
    recorded and permanently invisible.
    """
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github)
    fixture.queue.enqueue(opened(), now=NOW)

    for _ in range(5):
        await fixture.worker.run_once()

    assert fixture.queue.status(_publish_key()) is QueueStatus.PENDING
    assert len(fixture.engine.requests) == 1

    github._write_status = 201
    await fixture.worker.run_once()

    assert posted_comment(fixture.store, opened().dedupe_key) == 555
    assert fixture.queue.status(_publish_key()) is QueueStatus.DONE


async def test_posts_are_given_up_on_at_the_bound(wired, git_remote, caplog):
    """Issue #71: a post GitHub will never accept stops being retried.

    Every claim used to repeat it, because a publish-only retry counts no
    attempt -- so the review stayed invisible and an ERROR was logged every
    idle period, for the lifetime of the database.
    """
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github, max_publish_attempts=3)
    fixture.queue.enqueue(opened(), now=NOW)

    with caplog.at_level(logging.ERROR):
        for _ in range(4):
            await fixture.worker.run_once()

    attempts, failed_at = publish_state(fixture.store, opened().dedupe_key)
    assert (attempts, failed_at is not None) == (3, True)
    assert fixture.queue.status(_publish_key()) is QueueStatus.DONE
    assert "gave up posting" in caplog.text
    # And nothing is left to claim, however long the daemon runs.
    assert await fixture.worker.run_once() is False


async def test_a_run_given_up_on_is_not_re_enqueued(wired, git_remote):
    """The first failed post is the last one when the bound is one."""
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github, max_publish_attempts=1)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(_publish_key()) is None
    attempts, failed_at = publish_state(fixture.store, opened().dedupe_key)
    assert (attempts, failed_at is not None) == (1, True)


async def test_clearing_the_stamp_publishes_the_review(wired, git_remote):
    """What an operator does once the reason the post failed is fixed."""
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github, max_publish_attempts=1)
    fixture.queue.enqueue(opened(), now=NOW)
    await fixture.worker.run_once()

    github._write_status = 201
    with fixture.store.transaction() as conn:
        conn.execute("UPDATE runs SET publish_failed_at = NULL, publish_attempts = 0")
    fixture.queue.enqueue(
        publication_of(opened(), _run_named(opened().dedupe_key)), now=NOW
    )

    await fixture.worker.run_once()

    assert posted_comment(fixture.store, opened().dedupe_key) == 555


async def test_a_publication_whose_lease_lapsed_posts_nothing(wired, git_remote):
    """The owner guards on `complete` run after the write; this runs before."""
    fixture = wired()
    run = fixture.runs.record(
        opened(), head_sha=git_remote.head_sha, result=recorded_result(), now=NOW
    )
    fixture.queue.enqueue(publication_of(opened(), run), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.worker.admit)
    stale = replace(claim, owner="a-worker-that-died")

    await fixture.worker.run_one(stale)

    assert fixture.github.comments == []


async def test_a_publication_item_for_a_posted_run_closes_itself(wired, git_remote):
    """Two items can name runs the other already posted; neither loops."""
    fixture = wired()
    run = fixture.runs.record(
        opened(), head_sha=git_remote.head_sha, result=recorded_result(), now=NOW
    )
    fixture.runs.mark_published(
        run.dedupe_key, comment_id=555, now=NOW, outcome="published"
    )
    item = publication_of(opened(), run)
    fixture.queue.enqueue(item, now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.comments == []
    assert fixture.queue.status(item.dedupe_key) is QueueStatus.DONE


async def test_a_lapsed_lease_publishes_nothing(wired):
    """`settle` returning False is how a worker learns to discard a result.

    It now discards a comment under the agent's own account, not just a
    number.
    """
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    stale = replace(claim, owner="a-worker-that-died")

    await fixture.worker.run_one(stale)

    assert fixture.github.comments == []


@pytest.mark.parametrize("outcome", [Outcome.TRUNCATED, Outcome.FAILED])
async def test_an_unfinished_run_publishes_nothing(wired, outcome):
    """Only a completed run carries publishable findings."""
    fixture = wired(engine=OutcomeEngine(outcome=outcome))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.comments == []
    assert fixture.runs.unpublished(opened().dedupe_key) is None


async def test_a_dry_run_reviews_and_posts_nothing(wired):
    """The full pipeline, spending the same tokens, with no comment."""
    fixture = wired(dry_run=True)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.engine.requests
    assert fixture.github.comments == []
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE


async def test_a_dry_run_is_not_re_offered_forever(wired):
    fixture = wired(dry_run=True)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.runs.unpublished(opened().dedupe_key) is None
