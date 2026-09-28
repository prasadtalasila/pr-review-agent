"""What the worker will and will not claim, and what each ending reads as.

The admission predicate runs inside the claim transaction, so these cases
are about the two decisions that cannot be separated: whether a trigger is
taken at all, and what the run that follows settles.
"""

from worker_harness import (
    MAX_RUN_TOKENS,
    NOW,
    POSIX_ONLY,
    ExplodingEngine,
    SpyQueue,
    TimingOutEngine,
    client_returning,
    ledger_rows,
    mention,
    opened,
    posted_comment,
    recorded_result,
    stop_reasons,
)

from pr_review_agent.budget import Mode, StopReason, UsageConfidence
from pr_review_agent.queue import QueueStatus
from pr_review_agent.runs import publication_of

pytestmark = POSIX_ONLY


# -- the spending rail ---------------------------------------------------


async def test_a_claim_is_never_taken_without_an_admit_predicate(wired):
    """CLAUDE.md section 5, pinned by a test rather than held by review."""
    fixture = wired(queue_class=SpyQueue)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert isinstance(fixture.queue, SpyQueue)
    assert fixture.queue.admits == [fixture.worker.admit]


async def test_a_refusal_reaches_no_engine(wired):
    """The predicate is the governor's for anything that could spend."""
    fixture = wired()
    fixture.worker.governor.admit = lambda conn, claim, now: False
    fixture.queue.enqueue(opened(), now=NOW)

    assert await fixture.worker.run_once() is False
    assert fixture.engine.requests == []
    assert ledger_rows(fixture.store) == []


async def test_an_exhausted_budget_still_admits_a_publication_item(wired, git_remote):
    """An exhausted budget must not hold a paid review hostage.

    The allowance this run cost was spent days ago and has already settled.
    Posting it reaches no engine, so weighing it against a window would be
    refusing to spend nothing.
    """
    fixture = wired()
    run = fixture.runs.record(
        opened(), head_sha=git_remote.head_sha, result=recorded_result(), now=NOW
    )
    fixture.queue.enqueue(publication_of(opened(), run), now=NOW)
    fixture.worker.governor.admit = lambda conn, claim, now: False

    assert await fixture.worker.run_once() is True

    assert fixture.engine.requests == []  # nothing was reviewed
    assert ledger_rows(fixture.store) == []  # and nothing was reserved
    assert posted_comment(fixture.store, opened().dedupe_key) == 555


async def test_an_unposted_run_does_not_admit_a_fresh_review(wired, git_remote):
    """The exemption is read off the item, never off the pull request.

    A recorded, unposted run used to admit *any* claim for its pull request
    without reserving -- so a fresh mention arriving under an exhausted
    budget was let through, and then spent on republishing that older run
    instead of reviewing the head it was written against.
    """
    fixture = wired()
    fixture.runs.record(
        opened(), head_sha=git_remote.head_sha, result=recorded_result(), now=NOW
    )
    fixture.queue.enqueue(mention(), now=NOW)
    fixture.worker.governor.admit = lambda conn, claim, now: False

    assert await fixture.worker.run_once() is False

    assert fixture.github.comments == []  # the old run was not posted
    assert fixture.queue.status(mention().dedupe_key) is QueueStatus.PENDING


async def test_a_mention_reviews_its_own_head_while_a_run_waits_to_be_posted(
    wired, git_remote
):
    """The superseded-run swallow: a mention is a review request, always."""
    fixture = wired()
    fixture.runs.record(
        opened(), head_sha=git_remote.head_sha, result=recorded_result(), now=NOW
    )
    fixture.queue.enqueue(mention(), now=NOW)

    await fixture.worker.run_once()

    assert len(fixture.engine.requests) == 1  # it was reviewed, not republished
    assert fixture.queue.status(mention().dedupe_key) is QueueStatus.DONE


async def test_a_timed_out_run_is_distinguishable_from_a_crashed_one(wired):
    """The one per-run ceiling the agent enforces is the one worth counting."""
    fixture = wired(engine=TimingOutEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.TIMEOUT)]
    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, _, _ = row
    # Unchanged by the stop_reason work: a killed process printed nothing, so
    # the run is charged its ceiling at a confidence that says we did not
    # measure it.
    assert (used, confidence) == (reserved, str(UsageConfidence.UNAVAILABLE))


async def test_an_engine_that_fell_over_reads_as_an_engine_error(wired):
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.ENGINE_ERROR)]


async def test_a_clean_review_reads_as_completed(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.COMPLETED)]


async def test_a_github_failure_before_the_engine_reads_as_infrastructure(wired):
    fixture = wired(client=client_returning(None, status=500))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.INFRASTRUCTURE)]


async def test_nothing_to_claim_runs_nothing(wired):
    fixture = wired()
    assert await fixture.worker.run_once() is False
    assert ledger_rows(fixture.store) == []


# -- the successful path -------------------------------------------------


async def test_a_review_settles_what_the_engine_reported(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    assert await fixture.worker.run_once() is True

    (row,) = ledger_rows(fixture.store)
    key, mode, reserved, used, confidence, engine, model = row
    assert key == opened().dedupe_key
    assert mode == str(Mode.FULL)
    assert reserved == MAX_RUN_TOKENS
    assert (used, confidence) == (1_000, str(UsageConfidence.EXACT))
    assert (engine, model) == ("fake", "fake-1")


async def test_a_reviewed_row_is_done(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE
