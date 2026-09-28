"""The queue suite's two triggers and the store they are enqueued in.

Shared by the `test_queue_*.py` family: a pull request and a mention on the
same pull request, which is the pair most of the protocol is about.
"""

from datetime import datetime, timezone

import pytest

from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import CommentSource, Trigger, TriggerKind

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
REPO = "prasadtalasila/pr-review-agent"


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


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield store


@pytest.fixture(name="queue")
def queue_fixture(store):
    return ReviewQueue(store, repo=REPO)
