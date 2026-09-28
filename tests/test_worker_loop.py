"""The drainer around one review: the loop, the lease, and the preflight.

A review runs for minutes and the loop must not stop for it; a lease that
lapsed underneath one must throw its result away.
"""

import asyncio
from dataclasses import dataclass

from worker_harness import NOW, POSIX_ONLY, ExplodingEngine, budget, ledger_rows, opened

from pr_review_agent.budget import Governor, StopReason, Usage, UsageConfidence
from pr_review_agent.engine import FakeEngine, ReviewRequest
from pr_review_agent.engine.models import ReviewResult
from pr_review_agent.poller.client import GitHubClientError
from pr_review_agent.queue import Claim, QueueStatus

pytestmark = POSIX_ONLY


# -- the loop ------------------------------------------------------------


async def test_the_loop_stops_when_asked(wired):
    fixture = wired()
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(fixture.worker.run_forever(stop), timeout=5)


async def test_the_loop_drains_what_is_queued(wired, monkeypatch):
    monkeypatch.setattr("pr_review_agent.worker.WORKER_IDLE", 0.01)
    fixture = wired()
    fixture.queue.enqueue(opened(head_sha="aaa"), now=NOW)
    fixture.queue.enqueue(opened(head_sha="bbb"), now=NOW)
    stop = asyncio.Event()

    async def until_drained():
        while (
            fixture.queue.status("pr_opened:owner/name:7:bbb") is not QueueStatus.DONE
        ):
            await asyncio.sleep(0.01)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(fixture.worker.run_forever(stop), until_drained()), timeout=30
    )
    assert fixture.worker.completed == 2


async def test_a_finished_run_is_counted(wired):
    """The supervisor reads this to tell progress from a crash loop."""
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.worker.completed == 1


async def test_a_failed_run_is_not_counted_as_progress(wired):
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.worker.completed == 0


# -- the governor's refusal ----------------------------------------------


async def test_a_refused_claim_leaves_the_row_alone(wired):
    fixture = wired(config=budget(enabled=False))
    fixture.queue.enqueue(opened(), now=NOW)

    assert await fixture.worker.run_once() is False

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING
    assert ledger_rows(fixture.store) == []


async def test_usage_reported_by_the_engine_is_what_is_recorded(wired):
    fixture = wired(
        engine=FakeEngine(
            usage=Usage(
                tokens=42,
                confidence=UsageConfidence.ESTIMATED,
                engine="fake",
                model="fake-2",
            )
        )
    )
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    assert (row[3], row[4], row[6]) == (42, str(UsageConfidence.ESTIMATED), "fake-2")


def test_the_client_error_type_is_what_the_worker_retries():
    """Guard against the import being dropped: the taxonomy depends on it."""
    assert issubclass(GitHubClientError, RuntimeError)


@dataclass
class SlowEngine(FakeEngine):
    """An engine that takes its time, as a real one does."""

    delay: float = 0.2

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Wait, then answer -- yielding the event loop while it waits."""
        await asyncio.sleep(self.delay)
        return await super().review(request)


async def test_a_running_review_does_not_block_the_rest_of_the_loop(wired, monkeypatch):
    """Issue #16: the poll cycle must keep cycling while a review runs."""
    monkeypatch.setattr("pr_review_agent.worker.WORKER_IDLE", 0.01)
    fixture = wired(engine=SlowEngine())
    fixture.queue.enqueue(opened(), now=NOW)
    stop = asyncio.Event()
    cycles = 0

    async def other_loop():
        nonlocal cycles
        while fixture.worker.completed == 0:
            cycles += 1
            await asyncio.sleep(0.01)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(fixture.worker.run_forever(stop), other_loop()), timeout=30
    )

    assert fixture.worker.completed == 1
    assert cycles > 5


@dataclass
class StealingEngine(FakeEngine):
    """An engine whose run outlives the reservation it was admitted under.

    It settles the row mid-review, which is what a worker that re-claimed a
    lapsed lease would have done by the time this run finished.
    """

    governor: Governor | None = None
    claim: object = None

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Settle the reservation out from under the caller, then answer."""
        assert self.governor is not None and isinstance(self.claim, Claim)
        self.governor.settle(
            self.claim,
            Usage(7, UsageConfidence.EXACT, engine="other", model="other-1"),
            now=NOW,
            stop_reason=StopReason.COMPLETED,
        )
        return await super().review(request)


async def test_a_lease_lost_mid_review_discards_the_result(wired):
    """The run finished, but this worker is no longer the one entitled to it."""
    fixture = wired(engine=StealingEngine())
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    assert claim is not None
    engine = fixture.worker.engine
    assert isinstance(engine, StealingEngine)
    engine.governor, engine.claim = fixture.governor, claim

    await fixture.worker.run_one(claim)

    # Not done: finishing here would close a row this worker no longer holds.
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.CLAIMED
    (row,) = ledger_rows(fixture.store)
    assert (row[3], row[5]) == (7, "other")


# -- the pre-flight estimate: the last free refusal -----------------------


async def test_a_run_predicted_to_overrun_never_reaches_the_engine(wired):
    """budget.preflight is free to refuse: no engine has run, no tokens gone."""
    # One reviewable line against a 1-token ceiling: the prediction cannot fit.
    fixture = wired(config=budget(max_run_tokens=1))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    assert engine.requests == []


async def test_a_preflight_refusal_settles_at_zero_and_is_not_retried(wired):
    """preflight released the hold itself; the row is deterministic, so it ends."""
    fixture = wired(config=budget(max_run_tokens=1))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    assert (row[3], row[4]) == (0, str(UsageConfidence.EXACT))
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


async def test_the_engine_is_shown_only_what_survived_the_exclusions(wired):
    """The checkout is built with budget.excluded_paths, not without them."""
    fixture = wired(config=budget(excluded_paths=("feature.py",)))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    # Everything this pull request changes is excluded, so there is nothing
    # left to review and preflight refuses before the engine.
    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    assert engine.requests == []
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED
