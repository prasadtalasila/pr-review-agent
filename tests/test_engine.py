"""The engine seam: the contract, and the conformance every engine owes it."""

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pr_review_agent.budget import Governor, Mode, StopReason, Usage, UsageConfidence
from pr_review_agent.config import BudgetConfig
from pr_review_agent.engine import (
    FULL,
    ClaudeCliEngine,
    FakeEngine,
    Finding,
    Outcome,
    ReviewEngine,
    ReviewRequest,
    ReviewResult,
    Severity,
)
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import Trigger, TriggerKind
from pr_review_agent.workspace import Checkout, DiffSize, PullRequestFacts

FACTS = PullRequestFacts(
    number=7,
    head_sha="a" * 40,
    base_ref="main",
    additions=10,
    deletions=2,
    changed_files=1,
)

TRIGGER = Trigger(
    kind=TriggerKind.PR_OPENED,
    repo="o/r",
    pr_number=7,
    head_sha="a" * 40,
    actor_id=114395272,
    dedupe_key="o/r#7@" + "a" * 40,
)


def request(tmp_path: Path) -> ReviewRequest:
    checkout = Checkout(
        path=tmp_path,
        head_sha="a" * 40,
        merge_base="b" * 40,
        diff="--- a/x\n+++ b/x\n",
        reviewed=DiffSize(files=1, lines=1),
    )
    return ReviewRequest(
        checkout=checkout, facts=FACTS, trigger=TRIGGER, mode=Mode.FULL
    )


class StubbedClaude(ClaudeCliEngine):
    """The real adapter with the subprocess taken out.

    Every rule below is about what an engine returns, not about how it got
    there, so the adapter can sit in the conformance suite without a binary,
    a network or a token.
    """

    ENVELOPE = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "usage": {"input_tokens": 900, "output_tokens": 100},
            "structured_output": {"findings": []},
        }
    )

    async def preflight(self) -> None:
        """No binary to ask for a version."""

    async def run(self, argv, prompt, *, cwd) -> str:
        """Return a recorded envelope instead of running anything."""
        del argv, prompt, cwd
        return self.ENVELOPE

    async def prompt(self, request) -> str:
        """Skip the git read; the prompt is not what these rules are about."""
        del request
        return "review the diff"


# Every engine in the suite. A second adapter joins this list rather than
# growing its own copy of the rules below.
ENGINES = [
    FakeEngine(),
    FakeEngine(
        name="silent",
        capabilities=dataclasses.replace(FULL, usage_reporting=False),
        usage=Usage(tokens=0, confidence=UsageConfidence.UNAVAILABLE, engine="silent"),
    ),
    StubbedClaude(model="claude-sonnet-5", expected_version="2.1.274"),
]


# -- the conformance suite --


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e.name)
async def test_engine_satisfies_the_protocol(engine):
    assert isinstance(engine, ReviewEngine)


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e.name)
async def test_result_usage_is_attributable_to_the_engine(engine, tmp_path):
    """A cost nobody can attribute cannot be settled against a window."""
    result = await engine.review(request(tmp_path))
    assert result.usage.engine


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e.name)
async def test_declared_usage_reporting_matches_what_is_returned(engine, tmp_path):
    """The capability is a promise about the ledger row, so it must hold.

    An engine that declares no usage reporting but settles as `exact` would
    put an invented token count into the rolling windows; one that declares
    reporting and settles as `unavailable` would silently drop a real cost.
    """
    result = await engine.review(request(tmp_path))
    if engine.capabilities.usage_reporting:
        assert result.usage.confidence is not UsageConfidence.UNAVAILABLE
    else:
        assert result.usage.confidence is UsageConfidence.UNAVAILABLE


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e.name)
async def test_findings_are_published_only_by_a_completed_run(engine, tmp_path):
    """A run that was cut off did not review the pull request."""
    result = await engine.review(request(tmp_path))
    assert result.outcome is Outcome.COMPLETED or result.findings == ()


# -- the result invariants --


def test_unavailable_usage_cannot_also_report_tokens():
    # An unknown cost and a number are different answers. A row claiming
    # both would draw down a window on a measurement nobody made.
    with pytest.raises(ValueError, match="unavailable"):
        ReviewResult(
            findings=(),
            usage=Usage(
                tokens=5_000,
                confidence=UsageConfidence.UNAVAILABLE,
                engine="fake",
            ),
        )


def test_unavailable_usage_with_no_tokens_is_fine():
    result = ReviewResult(
        findings=(),
        usage=Usage(tokens=0, confidence=UsageConfidence.UNAVAILABLE, engine="fake"),
    )
    assert result.usage.tokens == 0


@pytest.mark.parametrize("outcome", [Outcome.TRUNCATED, Outcome.FAILED])
def test_a_run_that_did_not_complete_cannot_carry_findings(outcome):
    # "The publisher may post these" is a property of the seam, so it is
    # enforced where the result is built rather than in each adapter.
    with pytest.raises(ValueError, match="publishable findings"):
        ReviewResult(
            findings=(
                Finding(path="x", line=1, severity=Severity.NIT, body="stopped early"),
            ),
            usage=Usage(tokens=10, confidence=UsageConfidence.EXACT, engine="fake"),
            outcome=outcome,
        )


def test_a_run_that_did_not_complete_still_carries_its_usage():
    """The money is spent whatever the run concluded."""
    result = ReviewResult(
        findings=(),
        usage=Usage(tokens=10, confidence=UsageConfidence.EXACT, engine="fake"),
        outcome=Outcome.TRUNCATED,
    )
    assert result.usage.tokens == 10


@pytest.mark.parametrize("engine_name", [None, ""])
def test_result_without_an_engine_name_is_rejected(engine_name):
    with pytest.raises(ValueError, match="must name the engine"):
        ReviewResult(
            findings=(),
            usage=Usage(
                tokens=10,
                confidence=UsageConfidence.EXACT,
                engine=engine_name,
            ),
        )


# -- the fake --


async def test_fake_returns_its_canned_findings(tmp_path):
    finding = Finding(
        path="src/x.py", line=12, severity=Severity.MAJOR, body="unbounded loop"
    )
    engine = FakeEngine(findings=(finding,))
    result = await engine.review(request(tmp_path))
    assert result.findings == (finding,)
    assert result.usage.tokens == 1_000


async def test_fake_records_every_request_it_was_handed(tmp_path):
    """So a test can assert what the worker passed, not that it passed."""
    engine = FakeEngine()
    await engine.review(request(tmp_path))
    await engine.review(request(tmp_path))
    assert len(engine.requests) == 2
    assert engine.requests[0].mode is Mode.FULL
    assert engine.requests[0].trigger is TRIGGER


async def test_fake_result_settles_against_the_governor(tmp_path):
    """The seam's point: the whole spending path exercised, costing nothing.

    `ReviewResult.usage` is `budget.Usage` itself, so this is the proof that
    "carries everything `settle` needs" is structural rather than asserted.
    """
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    with SqliteStore(tmp_path / "state.db") as store:
        governor = Governor(
            store,
            BudgetConfig(
                session_tokens=25_000, weekly_tokens=25_000, max_run_tokens=1_000
            ),
        )
        queue = ReviewQueue(store)
        queue.enqueue(TRIGGER, now=now)
        claim = queue.claim(now=now, owner="w", admit=governor.admit)
        assert claim is not None

        result = await FakeEngine().review(request(tmp_path))
        assert (
            governor.settle(
                claim,
                result.usage,
                now=now,
                stop_reason=StopReason.COMPLETED,
            )
            is True
        )


# -- the request --


def test_request_carries_the_diff_only_once(tmp_path):
    """Two copies of one string are two things that can disagree."""
    assert not hasattr(request(tmp_path), "diff")
    assert request(tmp_path).checkout.diff
