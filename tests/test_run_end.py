"""The failure taxonomy as a table: one row per thing that can go wrong.

`classify_failure` is the pure half of what used to be a six-armed
try/except inside a 190-line `run_one`. What it decides is a spending
question -- settle at nothing, at what the engine reported, or at the
ceiling this run reserved -- so each arm is worth stating directly rather
than reaching through a wired worker and a real checkout.
"""

import pytest

from pr_review_agent.budget import StopReason, Usage, UsageConfidence
from pr_review_agent.engine import EngineUnavailable, Outcome, UsageLimited
from pr_review_agent.poller.client import GitHubClientError
from pr_review_agent.triggers.models import PayloadError
from pr_review_agent.worker import (
    EngineError,
    Finish,
    _finish_for,
    classify_failure,
)
from pr_review_agent.workspace import (
    PullRequestFacts,
    PullRequestTooLarge,
    WorkspaceError,
)

FACTS = PullRequestFacts(
    number=7,
    head_sha="abc123",
    base_ref="main",
    additions=9_000,
    deletions=1_000,
    changed_files=900,
    state="open",
)


def too_large() -> PullRequestTooLarge:
    return PullRequestTooLarge("changed lines", 10_000, 5_000, FACTS)


#: What the run would settle at if the failure says nothing more precise.
#: The ceiling, so an arm that must override it is visible when it does.
CEILING = Usage(1_000, UsageConfidence.UNAVAILABLE, engine="fake")


@pytest.mark.parametrize(
    ("exc", "tokens", "reason", "finish"),
    [
        (
            too_large(),
            1_000,
            StopReason.INFRASTRUCTURE,
            Finish.ABANDON,
        ),
        (PayloadError("unusable"), 1_000, StopReason.INFRASTRUCTURE, Finish.ABANDON),
        (
            UsageLimited("out of quota"),
            0,
            StopReason.USAGE_LIMIT,
            Finish.RELEASE_UNATTEMPTED,
        ),
        (
            EngineUnavailable("no binary"),
            0,
            StopReason.ENGINE_UNAVAILABLE,
            Finish.RELEASE,
        ),
        (
            EngineError("died", StopReason.TIMEOUT),
            1_000,
            StopReason.TIMEOUT,
            Finish.RELEASE,
        ),
        (
            GitHubClientError("502"),
            1_000,
            StopReason.INFRASTRUCTURE,
            Finish.RELEASE,
        ),
        (
            WorkspaceError("fetch failed"),
            1_000,
            StopReason.INFRASTRUCTURE,
            Finish.RELEASE,
        ),
    ],
)
def test_each_failure_settles_and_closes_its_row_the_same_way(
    exc, tokens, reason, finish
):
    end = classify_failure(exc, CEILING)

    assert end.usage.tokens == tokens
    assert end.reason is reason
    assert end.finish is finish
    assert end.reviewed is None  # nothing that failed produced findings


def test_an_oversized_pull_request_is_matched_before_a_workspace_error():
    """`PullRequestTooLarge` subclasses `WorkspaceError`; order decides.

    Matched the other way round it would be retried -- twice more, reserving
    allowance each time to reach the same refusal.
    """
    assert isinstance(too_large(), WorkspaceError)
    assert classify_failure(too_large(), CEILING).finish is Finish.ABANDON


def test_a_usage_limit_that_measured_itself_settles_at_what_it_spent():
    """Knowable in both shapes: measured, or a provable zero."""
    measured = Usage(640, UsageConfidence.EXACT, engine="fake")

    end = classify_failure(UsageLimited("out of quota", measured), CEILING)

    assert end.usage == measured


def test_a_failure_before_the_engine_settles_at_nothing():
    """The reserved floor is the caller's; nothing here raises it."""
    nothing = Usage(0, UsageConfidence.UNAVAILABLE, engine="fake")

    assert classify_failure(GitHubClientError("502"), nothing).usage.tokens == 0


@pytest.mark.parametrize(
    ("outcome", "finish"),
    [
        (Outcome.COMPLETED, Finish.COMPLETE),
        (Outcome.TRUNCATED, Finish.RELEASE),
        (Outcome.FAILED, Finish.ABANDON),
    ],
)
def test_a_finished_run_closes_its_row_by_outcome(outcome, finish):
    """Truncated is worth another attempt's allowance; failed is not."""
    assert _finish_for(outcome) is finish
