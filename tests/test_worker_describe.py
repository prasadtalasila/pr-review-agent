"""``@claude describe`` through the worker: paid like a review, not a round.

The CLAUDE.md section 5 bound lives here. A description is a new thing a
comment can make the agent spend on, so it has to be held by every rail a
review is: the governor reserves and settles it, the brake refuses it, and
it counts against the per-pull-request cap -- it is not a way around one.
"""

import json
from dataclasses import replace

from worker_harness import (
    NOW,
    POSIX_ONLY,
    PR,
    REPO,
    budget,
    ledger_rows,
    mention,
    opened,
    reviewed_lines,
)

from pr_review_agent.description import ChangeType, Description, FileChange
from pr_review_agent.engine import FakeEngine, Finding, Severity
from pr_review_agent.queue import QueueStatus
from pr_review_agent.triggers.models import Command

pytestmark = POSIX_ONLY

DESCRIPTION = Description(
    type=ChangeType.ENHANCEMENT,
    summary="Adds a feature.",
    files=(FileChange(path="feature.py", change="The feature."),),
    testing="Run the tests.",
)
FINDING = Finding(
    path="feature.py", line=1, severity=Severity.MAJOR, title="A problem.", body="b"
)


def describe(comment_id=100):
    return replace(mention(comment_id), command=Command.DESCRIBE)


def _bodies(fixture) -> list[str]:
    """Every comment posted, leaving out the 👀 on the mention's comment."""
    posts = [c for c in fixture.github.comments if c.url.path.endswith("/comments")]
    return [json.loads(c.content)["body"] for c in posts]


def _engine() -> FakeEngine:
    return FakeEngine(findings=(FINDING,), description=DESCRIPTION)


async def test_a_description_is_posted_as_one_comment(wired):
    fixture = wired(engine=_engine())
    fixture.queue.enqueue(describe(), now=NOW)

    await fixture.worker.run_once()

    (body,) = _bodies(fixture)
    assert body.startswith(f"## Description: PR #{PR} ")
    assert "| `feature.py` | The feature. |" in body
    assert "## Blocking" not in body and "## Should fix" not in body
    assert fixture.queue.status(describe().dedupe_key) is QueueStatus.DONE


async def test_the_engine_is_asked_for_a_description_of_the_whole_change(wired):
    fixture = wired(engine=_engine())
    fixture.queue.enqueue(opened(), now=NOW)
    await fixture.worker.run_once()
    fixture.queue.enqueue(describe(), now=NOW)

    await fixture.worker.run_once()

    request = fixture.engine.requests[1]
    assert request.trigger.command is Command.DESCRIBE
    assert request.prior == ()
    assert request.checkout.since_sha is None


async def test_a_description_is_not_a_review_round(wired):
    fixture = wired(engine=_engine())
    for trigger in (opened(), describe(), mention()):
        fixture.queue.enqueue(trigger, now=NOW)
        await fixture.worker.run_once()

    review_one, _, review_two = _bodies(fixture)
    assert "round 1" in review_one and "round 2" in review_two
    # The review after the description is shown round 1's findings, not the
    # description's empty set.
    assert [f.number for f in fixture.engine.requests[2].prior] == [1]
    assert fixture.runs.history(REPO, PR).high_water == 2


async def test_a_description_is_charged_and_kept_out_of_the_fit(wired):
    fixture = wired(engine=_engine())
    fixture.queue.enqueue(describe(), now=NOW)

    await fixture.worker.run_once()

    ((key, _, reserved, used, *_),) = ledger_rows(fixture.store)
    assert key == describe().dedupe_key
    assert reserved > 0 and used == 1_000
    assert reviewed_lines(fixture.store) == [None]


async def test_a_description_counts_against_the_per_pull_request_cap(wired):
    fixture = wired(engine=_engine(), config=budget(max_reviews_per_pull_request=1))
    fixture.queue.enqueue(opened(), now=NOW)
    await fixture.worker.run_once()
    fixture.queue.enqueue(describe(), now=NOW)

    assert await fixture.worker.run_once() is False
    assert len(fixture.engine.requests) == 1
    assert fixture.queue.status(describe().dedupe_key) is QueueStatus.PENDING


async def test_the_brake_refuses_a_description(wired):
    fixture = wired(engine=_engine(), config=budget(enabled=False))
    fixture.queue.enqueue(describe(), now=NOW)

    assert await fixture.worker.run_once() is False
    assert fixture.engine.requests == []
