"""What a queued trigger asked for survives the queue, and bounds the fold.

``@claude describe`` and ``@claude review`` on one pull request are two
questions: a description does not answer a review request, and folding one
into the other would close a request nobody answered.
"""

from dataclasses import replace
from datetime import timedelta

from queue_harness import NOON, mention, opened

from pr_review_agent.queue import QueueStatus
from pr_review_agent.triggers.models import Command


def describe(comment_id: int):
    return replace(mention(comment_id=comment_id), command=Command.DESCRIBE)


def test_a_claim_carries_the_command(queue):
    queue.enqueue(describe(1), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None
    assert claim.trigger.command is Command.DESCRIBE


def test_a_trigger_without_one_is_a_review(queue):
    queue.enqueue(opened(), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None
    assert claim.trigger.command is Command.REVIEW


def test_a_description_folds_only_other_descriptions(queue):
    queue.enqueue(describe(1), now=NOON)
    queue.enqueue(describe(2), now=NOON)
    queue.enqueue(mention(comment_id=3), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None

    folded = queue.fold(claim, before=NOON + timedelta(seconds=1), head_sha="abc123")

    assert folded == 1
    assert queue.status(describe(2).dedupe_key) is QueueStatus.DONE
    assert queue.status(mention(comment_id=3).dedupe_key) is QueueStatus.PENDING


def test_a_review_leaves_a_waiting_description(queue):
    queue.enqueue(mention(comment_id=1), now=NOON)
    queue.enqueue(describe(2), now=NOON)
    claim = queue.claim(now=NOON, owner="w")
    assert claim is not None

    assert queue.fold(claim, before=NOON + timedelta(seconds=1), head_sha="x") == 0
    assert queue.status(describe(2).dedupe_key) is QueueStatus.PENDING
