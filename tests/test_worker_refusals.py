"""A refusal the contributor can read, and the retry policy behind it.

A refused review that says nothing is indistinguishable from an agent that
is down, so a refusal posts a notice -- once -- and the retry rules decide
which endings are worth a second attempt.
"""

import json

from worker_harness import (
    NOW,
    POSIX_ONLY,
    ExplodingEngine,
    GitHubDouble,
    OutcomeEngine,
    budget,
    ledger_rows,
    opened,
    stop_reasons,
)

from pr_review_agent.budget import StopReason, UsageConfidence
from pr_review_agent.engine.models import Outcome
from pr_review_agent.queue import QueueStatus

pytestmark = POSIX_ONLY


# -- a deterministic refusal says so on the pull request ------------------
#
# Issue #78: the 👀 goes on right after the claim, minutes before anyone
# knows a review is possible. When the size gate or the pre-flight then
# refuses, the row is abandoned -- and until now the contributor's whole
# experience was an acknowledgement followed by silence, with the reason
# only in the operator's journal.


def notices(fixture) -> list[str]:
    """Every comment body the worker posted on the pull request."""
    return [json.loads(r.content)["body"] for r in fixture.github.comments]


async def test_an_oversized_pull_request_is_told_why_it_was_refused(wired):
    """The size gate, with the cap that fired and the numbers behind it."""
    fixture = wired(config=budget(max_changed_lines=1))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (body,) = notices(fixture)
    assert "Not reviewed" in body
    assert "max_changed_lines" in body
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


async def test_a_pull_request_with_nothing_left_to_review_is_told_so(wired):
    """The pre-flight's other arm: every changed line is excluded.

    The fixture's third changed path is a binary blob, which is one file of
    zero lines either way -- so excluding the source and the vendored tree
    leaves nothing for the engine to be shown.
    """
    fixture = wired(config=budget(excluded_paths=("feature.py", "**/vendor/**")))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (body,) = notices(fixture)
    assert "budget.excluded_paths" in body


async def test_a_run_refused_on_predicted_cost_is_told_which_ceiling(wired):
    fixture = wired(config=budget(max_run_tokens=1))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (body,) = notices(fixture)
    assert "budget.max_run_tokens" in body


async def test_a_refusal_notice_is_posted_once_and_the_row_stays_closed(wired):
    """One notice ends one trigger; nothing re-offers it to post a second."""
    fixture = wired(config=budget(max_run_tokens=1))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()
    await fixture.worker.run_once()

    assert len(notices(fixture)) == 1
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


async def test_a_failed_refusal_notice_leaves_the_row_abandoned(wired, git_remote):
    """A lost courtesy must not turn a free refusal into a retried failure."""
    github = GitHubDouble(git_remote.head_sha, write_status=500)
    fixture = wired(config=budget(max_run_tokens=1), github=github)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED
    (row,) = ledger_rows(fixture.store)
    assert row[3] == 0
    assert stop_reasons(fixture.store) == [str(StopReason.REFUSED)]


async def test_a_dry_run_refuses_without_writing_a_notice(wired):
    fixture = wired(config=budget(max_run_tokens=1), dry_run=True)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert notices(fixture) == []
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


async def test_a_transient_failure_posts_no_notice(wired):
    """It will be retried, and a review that is still coming explains itself."""
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert notices(fixture) == []
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING


# -- Outcome decides what the row becomes --------------------------------


async def test_a_truncated_run_is_retried(wired):
    """Cut off with work outstanding: worth another attempt, tighter."""
    fixture = wired(engine=OutcomeEngine(outcome=Outcome.TRUNCATED))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING


async def test_a_failed_run_is_not_retried(wired):
    """`Outcome.FAILED` is everything else that went wrong, which is not."""
    fixture = wired(engine=OutcomeEngine(outcome=Outcome.FAILED))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


async def test_a_run_that_did_not_complete_still_settles_what_it_spent(wired):
    """It spent money and produced nothing; the ledger records the money."""
    fixture = wired(engine=OutcomeEngine(outcome=Outcome.TRUNCATED))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    assert (row[3], row[4]) == (500, str(UsageConfidence.EXACT))


async def test_only_a_completed_run_counts_as_progress(wired):
    """The supervisor's backoff reset must not be fed by failures."""
    fixture = wired(engine=OutcomeEngine(outcome=Outcome.FAILED))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.worker.completed == 0
