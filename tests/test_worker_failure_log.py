"""What an operator reads when a run fails, and what the failure is charged.

Every failure the taxonomy expects is one ERROR line that says what failed,
which attempt it was and whether another is coming. The traceback is kept at
DEBUG: for an expected failure it says only where the adapter raised, and at
ERROR it buried the one line that mattered under twenty that did not.

A failure whose output the adapter could still read carries what that output
measured, and is charged that rather than the reservation. The reservation
is for spend nobody could measure; charging it for spend somebody did
writes tokens that were never used into the rolling windows.
"""

import logging
from dataclasses import dataclass

from worker_harness import (
    FULL,
    MAX_RUN_TOKENS,
    NOW,
    POSIX_ONLY,
    ExplodingEngine,
    UnstartableEngine,
    ledger_rows,
    opened,
)

from pr_review_agent.budget import Usage, UsageConfidence
from pr_review_agent.engine import EngineProtocolError, ReviewRequest
from pr_review_agent.engine.models import Capabilities, ReviewResult
from pr_review_agent.queue import DEFAULT_MAX_ATTEMPTS

pytestmark = POSIX_ONLY

WORKER = "pr_review_agent.worker"


@dataclass
class MeasuredFailureEngine:
    """An adapter that read an errored envelope, usage and all."""

    name: str = "measured"
    capabilities: Capabilities = FULL
    tokens: int = 1_234

    async def review(self, request: ReviewRequest) -> ReviewResult:
        del request
        raise EngineProtocolError(
            "measured exited 1: success: 404 model not found",
            status=404,
            usage=Usage(self.tokens, UsageConfidence.EXACT, engine=self.name),
        )


def errors(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == WORKER and r.levelno == logging.ERROR
    ]


async def run(fixture, caplog, attempts: int = 1) -> None:
    fixture.queue.enqueue(opened(), now=NOW)
    with caplog.at_level(logging.DEBUG, logger=WORKER):
        for _ in range(attempts):
            await fixture.worker.run_once()


# -- the log line ----------------------------------------------------------


async def test_an_engine_failure_is_one_error_line_without_a_traceback(wired, caplog):
    fixture = wired(engine=MeasuredFailureEngine())

    await run(fixture, caplog)

    (record,) = errors(caplog)
    assert record.exc_info is None
    message = record.getMessage()
    assert "404 model not found" in message
    assert f"attempt 1 of {DEFAULT_MAX_ATTEMPTS}" in message
    assert "will be retried" in message


async def test_the_traceback_is_still_there_at_debug(wired, caplog):
    fixture = wired(engine=ExplodingEngine())

    await run(fixture, caplog)

    assert any(
        r.levelno == logging.DEBUG and r.exc_info is not None for r in caplog.records
    )


async def test_the_last_attempt_does_not_promise_a_retry(wired, caplog):
    fixture = wired(engine=ExplodingEngine())

    await run(fixture, caplog, attempts=DEFAULT_MAX_ATTEMPTS)

    last = errors(caplog)[-1].getMessage()
    n = DEFAULT_MAX_ATTEMPTS
    assert f"attempt {n} of {n}" in last
    assert "will be retried" not in last
    assert "giving up" in last


async def test_an_engine_that_never_started_says_so_without_a_traceback(wired, caplog):
    fixture = wired(engine=UnstartableEngine())

    await run(fixture, caplog)

    (record,) = errors(caplog)
    assert record.exc_info is None
    assert "could not start unstartable" in record.getMessage()
    assert "/nowhere" in record.getMessage()  # the operator does get the detail


# -- what it is charged ----------------------------------------------------


async def test_a_failure_that_measured_its_spend_is_charged_that(wired, caplog):
    fixture = wired(engine=MeasuredFailureEngine())

    await run(fixture, caplog)

    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, _, _ = row
    assert reserved == MAX_RUN_TOKENS
    assert (used, confidence) == (1_234, str(UsageConfidence.EXACT))


async def test_a_failure_that_measured_nothing_is_still_charged_the_reservation(
    wired, caplog
):
    """Nothing was read, so nothing is known: the reservation stands."""
    fixture = wired(engine=ExplodingEngine())

    await run(fixture, caplog)

    (row,) = ledger_rows(fixture.store)
    assert row[3] == MAX_RUN_TOKENS
