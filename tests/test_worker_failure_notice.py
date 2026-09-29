"""A review that failed for good says so on the pull request -- once.

Before this, an engine that kept failing left the contributor with a 👀 and
then silence, and the reason only in the operator's journal. The notice is
posted when the last allowed attempt fails, not on each one: a transient
failure that the retry fixes has nothing to announce.

It names the *category* of failure and never the text the tool printed.
That text can carry host paths, account and quota state, or whatever the CLI
chose to echo, and this comment goes under the agent's account on what may
be a public repository.
"""

import json
from dataclasses import dataclass

import httpx
from worker_harness import (
    FULL,
    NOW,
    POSIX_ONLY,
    ExplodingEngine,
    TimingOutEngine,
    UnstartableEngine,
    opened,
)

from pr_review_agent.engine import EngineProtocolError, ReviewRequest, UsageLimited
from pr_review_agent.engine.models import Capabilities, ReviewResult
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.queue import DEFAULT_MAX_ATTEMPTS

pytestmark = POSIX_ONLY

#: What a real failure printed: nothing of it may reach the pull request.
CLI_TEXT = "claude exited 1: success: 404 There's an issue with /home/foo/.claude"


@dataclass
class ApiErrorEngine:
    """An adapter that read an errored envelope with an API status on it."""

    name: str = "api-error"
    capabilities: Capabilities = FULL

    async def review(self, request: ReviewRequest) -> ReviewResult:
        del request
        raise EngineProtocolError(CLI_TEXT, status=404)


@dataclass
class UsageLimitedEngine:
    """The account is out of quota: the breaker's, not the pull request's."""

    name: str = "limited"
    capabilities: Capabilities = FULL

    async def review(self, request: ReviewRequest) -> ReviewResult:
        del request
        raise UsageLimited("claude reports a usage limit")


def comments(fixture) -> list[str]:
    """Every comment body the worker posted on the pull request."""
    return [json.loads(r.content)["body"] for r in fixture.github.comments]


async def exhaust(fixture, attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
    fixture.queue.enqueue(opened(), now=NOW)
    for _ in range(attempts):
        await fixture.worker.run_once()


async def test_a_failure_with_attempts_left_posts_nothing(wired):
    """The retry may yet succeed, and then there is nothing to announce."""
    fixture = wired(engine=ExplodingEngine())

    await exhaust(fixture, attempts=DEFAULT_MAX_ATTEMPTS - 1)

    assert comments(fixture) == []


async def test_the_last_failed_attempt_posts_one_notice(wired):
    fixture = wired(engine=ExplodingEngine())

    await exhaust(fixture)

    (body,) = comments(fixture)
    assert "Not reviewed" in body
    assert "review engine failed" in body
    assert f"{DEFAULT_MAX_ATTEMPTS} attempts" in body
    assert "did not finish in time" not in body  # the exception's own text


async def test_the_notice_names_the_api_status_and_nothing_the_cli_printed(wired):
    fixture = wired(engine=ApiErrorEngine())

    await exhaust(fixture)

    (body,) = comments(fixture)
    assert "API error 404" in body
    assert "/home/foo" not in body
    assert "There's an issue" not in body


async def test_a_timed_out_engine_is_told_as_a_timeout(wired):
    fixture = wired(engine=TimingOutEngine())

    await exhaust(fixture)

    (body,) = comments(fixture)
    assert "ran out of time" in body
    assert "900" not in body


async def test_an_engine_that_never_started_is_told_so_without_its_path(wired):
    fixture = wired(engine=UnstartableEngine())

    await exhaust(fixture)

    (body,) = comments(fixture)
    assert "could not be started" in body
    assert "/nowhere" not in body


async def test_a_dry_run_posts_no_failure_notice(wired):
    """A comment under the agent's account is what the brake is on to stop."""
    fixture = wired(engine=ExplodingEngine(), dry_run=True)

    await exhaust(fixture)

    assert comments(fixture) == []


async def test_a_usage_limit_is_never_announced_on_the_pull_request(wired):
    """It spends no attempt, so it is never the last one; the breaker owns it."""
    fixture = wired(engine=UsageLimitedEngine())

    await exhaust(fixture)

    assert comments(fixture) == []


async def test_a_failure_that_never_reached_the_engine_is_not_announced(wired):
    """GitHub answering 500 is not the engine, and a post would likely fail too."""
    writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/comments" in request.url.path:
            writes.append(request)
        return httpx.Response(500, text="boom")

    fixture = wired(
        client=GitHubClient(token="t", transport=httpx.MockTransport(handler))
    )

    await exhaust(fixture)

    assert not writes
