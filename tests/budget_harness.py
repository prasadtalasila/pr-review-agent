"""The budget suite's fixtures: one store, one config, two triggers.

Split out of what was one module so that each `test_budget_*.py` states one
thing -- the windows, the ladder, the fit -- against wiring that is written
once. `test_budget_concurrency.py` drives the same governor from several
threads.
"""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.config import BudgetConfig
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import Trigger, TriggerKind

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
REPO = "prasadtalasila/pr-review-agent"

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
    queue = ReviewQueue(store, repo=REPO)
    claims = []
    for index, trigger in enumerate(triggers):
        queue.enqueue(trigger, now=now + timedelta(seconds=index))
    while (claim := queue.claim(now=now, owner="w", admit=governor.admit)) is not None:
        claims.append(claim)
        queue.complete(claim)
    return claims


def burn_to(governor, queue, mode):
    """Admit runs until the ladder reaches ``mode``."""
    index = 0
    while governor.headroom(NOON).mode is not mode:
        queue.enqueue(opened(pr=index), now=NOON + timedelta(seconds=index))
        claim = queue.claim(now=NOON, owner="w", admit=governor.admit)
        assert claim is not None, f"never reached {mode}"
        queue.complete(claim)
        index += 1
