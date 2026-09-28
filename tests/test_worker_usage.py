"""Usage: the limit, the ledger, and the estimate it feeds.

A usage limit is not a failed review -- it is an unattempted one, settled at
what is known rather than at the ceiling, and it must leave the row able to
run again.
"""

import pytest
from conftest import REVIEWABLE_LINES
from worker_harness import (
    MAX_RUN_TOKENS,
    NOW,
    POSIX_ONLY,
    OutcomeEngine,
    client_returning,
    ledger_rows,
    opened,
    reviewed_lines,
    stop_reasons,
)

from pr_review_agent.budget import (
    DEFAULT_TOKENS_PER_LINE,
    MIN_FIT_SAMPLES,
    StopReason,
    Usage,
    UsageConfidence,
)
from pr_review_agent.engine import (
    FULL,
    Capabilities,
    FakeEngine,
    ReviewRequest,
    UsageLimited,
)
from pr_review_agent.engine.models import Outcome, ReviewResult
from pr_review_agent.queue import QueueStatus

pytestmark = POSIX_ONLY


# -- the account's own limit -----------------------------------------------


class LimitedEngine:
    """An engine that hits the account's own limit rather than its own."""

    name: str = "limited"
    capabilities: Capabilities = FULL

    def __init__(self, usage: Usage | None = None) -> None:
        self.usage = usage
        self.calls = 0

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Refuse the way a CLI out of quota does."""
        del request
        self.calls += 1
        raise UsageLimited("the account is out of quota", self.usage)


async def test_a_usage_limit_trips_the_breaker(wired):
    """The one failure that must stop the next run rather than retry it."""
    fixture = wired(engine=LimitedEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.governor.breaker().tripped(NOW)
    assert fixture.governor.breaker().calibrated_pct < 100


async def test_a_usage_limit_settles_at_what_is_known_not_the_ceiling(wired):
    """Refused before doing work: charging the reservation would invent spend.

    Phantom tokens here are not cosmetic. The windows are rolling and ledger
    rows are never deleted, so a reservation's worth of usage that never
    happened keeps refusing real runs for up to a week after the account has
    recovered.
    """
    fixture = wired(engine=LimitedEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    assert row[2] == MAX_RUN_TOKENS  # reserved
    assert row[3] == 0  # used
    assert row[4] == str(UsageConfidence.EXACT)


async def test_a_usage_limit_hit_mid_run_settles_at_the_measured_figure(wired):
    """The envelope measured it, so the ledger records it."""
    fixture = wired(engine=LimitedEngine(Usage(321, UsageConfidence.EXACT, "limited")))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    assert row[3] == 321


async def test_a_usage_limit_is_recorded_as_its_own_stop_reason(wired):
    """`GROUP BY stop_reason` has to distinguish it from an engine failure."""
    fixture = wired(engine=LimitedEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.USAGE_LIMIT)]


async def test_a_usage_limit_leaves_the_row_unattempted(wired):
    """The account was already out when this trigger arrived: it drained
    nothing, so counting the attempt would abandon a good review."""
    fixture = wired(engine=LimitedEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING
    # And the breaker, not the queue, is what stops it being attempted again.
    assert await fixture.worker.run_once() is False
    assert fixture.engine.calls == 1


# -- what the pre-flight fit is fitted against ---------------------------
#
# Issue #32: the worker settled without the line count, so every row the
# running daemon wrote left `reviewed_lines` NULL -- and `_FIT_SAMPLE`
# selects on it being non-null. The fit could never reach MIN_FIT_SAMPLES,
# so the estimate stayed at its cold-start constant for ever. These tests
# drive it through the worker, which is the gap that made it invisible:
# test_budget.py's own fit tests call `settle` with the argument directly.


async def test_a_completed_review_records_the_lines_it_reviewed(wired):
    """The size the worker handed over, so the fit has something to fit."""
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert reviewed_lines(fixture.store) == [REVIEWABLE_LINES]


async def test_a_failure_before_the_engine_records_no_lines(wired):
    """Nothing was handed to an engine, so nothing was reviewed."""
    fixture = wired(client=client_returning(None, status=500))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert reviewed_lines(fixture.store) == [None]


@pytest.mark.parametrize("outcome", [Outcome.TRUNCATED, Outcome.FAILED])
async def test_a_run_that_did_not_finish_records_no_lines(wired, outcome):
    """A run cut off spent less than a full review of those lines costs.

    Fitting it would pull the rate down, which is the direction that
    under-refuses -- so only a finished review is a sample of what one costs.
    """
    fixture = wired(engine=OutcomeEngine(outcome=outcome))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert reviewed_lines(fixture.store) == [None]


async def test_a_usage_limited_run_records_no_lines(wired):
    """The worst of them: it settles at *exact* zero, which the fit admits.

    Rows saying a pull request cost nothing would fit a rate of nothing, and
    ledger rows are never deleted -- so the estimate would stop refusing
    anything, permanently.
    """
    fixture = wired(engine=LimitedEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert reviewed_lines(fixture.store) == [None]


async def test_the_fit_engages_once_the_worker_has_settled_enough_runs(wired):
    """End to end: the agent uses the evidence it collected about itself."""
    fixture = wired()
    for index in range(MIN_FIT_SAMPLES):
        fixture.queue.enqueue(opened(head_sha=f"sha{index}"), now=NOW)

    for _ in range(MIN_FIT_SAMPLES):
        assert await fixture.worker.run_once() is True

    assert reviewed_lines(fixture.store) == [REVIEWABLE_LINES] * MIN_FIT_SAMPLES
    # 1,000 tokens over REVIEWABLE_LINES lines, ten times over.
    rate = FakeEngine().usage.tokens / REVIEWABLE_LINES
    assert fixture.governor.estimate(100) == round(100 * rate)
    assert fixture.governor.estimate(100) != 100 * DEFAULT_TOKENS_PER_LINE
