"""The publisher suite's doubles: a recording transport and one recorded run.

Shared by the `test_publisher_*.py` family. `Transport` is the whole fake
GitHub -- it answers the head re-check and records every write, which is
what lets "no PATCH was ever sent" be an assertion about the wire rather
than about the code.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from pr_review_agent.budget import Usage, UsageConfidence
from pr_review_agent.comments import AgentComments
from pr_review_agent.config import PublishConfig
from pr_review_agent.engine import Finding, Outcome, ReviewResult, Severity
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.publisher import (
    Publisher,
    render,
)
from pr_review_agent.runs import RecordedRun, RunStore
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import CommentSource, Trigger, TriggerKind

NOON = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
REPO = "o/r"
HEAD = "deadbeef0123456789"
ENDPOINTS = RepoEndpoints(owner="o", name="r")

FINDINGS = (
    Finding(
        path="src/b.py",
        line=3,
        severity=Severity.NIT,
        title="A stray space trails the assignment.",
        body="stray space",
        number=2,
    ),
    Finding(
        path="src/a.py",
        line=12,
        severity=Severity.MAJOR,
        title="The file handle leaks when parsing raises.",
        body="leaks a handle",
        number=1,
    ),
)

#: A report with all three sections filled, numbered the way a third round
#: would be: 2, 9 and 11, with the gaps left by findings fixed in rounds 1
#: and 2. Modelled on the reference report the template was drawn from.
NUMBERED = (
    Finding(
        path="script/docs.sh",
        line=46,
        severity=Severity.BLOCKER,
        title=(
            "`script/docs.sh` copies an asset this PR deletes, "
            "so the docs build breaks."
        ),
        body=(
            "Line 46 still copies the logo.\n\n"
            "Update the publish path in the same commit."
        ),
        number=2,
    ),
    Finding(
        path="script/build_brand.py",
        line=14,
        severity=Severity.MINOR,
        title="The generators assume they are run from the repo root.",
        body=(
            "`build_brand.py` writes to a relative path.\n\n"
            "Resolve it against `__file__`."
        ),
        number=9,
    ),
    Finding(
        path="client/src/BrandMark.tsx",
        line=3,
        severity=Severity.NIT,
        title="Fixed clipPath ids collide when two marks share a document.",
        body="`useId()` would remove the trap.",
        number=11,
    ),
)


class Transport:
    """Records every request, and answers from a routing table."""

    def __init__(self, head=HEAD, comment_id=555, commits: int | None = 3):
        self.requests: list[httpx.Request] = []
        self._head = head
        self._comment_id = comment_id
        self._commits = commits
        self._posted = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            payload: dict = {"head": {"sha": self._head}}
            if self._commits is not None:
                payload["commits"] = self._commits
            return httpx.Response(200, json=payload)
        # A new id per write, the way GitHub answers a second POST: a test
        # that asserted one id everywhere could not tell a second comment
        # from a rewritten first one.
        self._posted += 1
        return httpx.Response(201, json={"id": self._comment_id + self._posted - 1})

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method in ("POST", "PATCH")]


def mention(comment_id=999, source=CommentSource.ISSUE):
    return Trigger(
        kind=TriggerKind.MENTION,
        repo=REPO,
        pr_number=7,
        head_sha=None,
        actor_id=99,
        dedupe_key="mention:o/r:7:999",
        comment_id=comment_id,
        comment_source=source,
    )


def opened():
    return Trigger(
        kind=TriggerKind.PR_OPENED,
        repo=REPO,
        pr_number=7,
        head_sha=HEAD,
        actor_id=99,
        dedupe_key=f"pr_opened:o/r:7:{HEAD}",
    )


def recorded(runs, findings=FINDINGS, head_sha=HEAD, key="k1", assessment=None):
    """Store a run the way the worker does, and hand back what it stored.

    Recording before publishing is the order the worker uses and the reason
    a failed publish can be retried, so the tests take the same route rather
    than handing the publisher a run the store has never seen.
    """
    runs.record(
        replace(opened(), dedupe_key=key),
        head_sha=head_sha,
        result=result_of(findings, assessment),
        now=NOON,
    )
    return RecordedRun(
        dedupe_key=key,
        repo=REPO,
        pr_number=7,
        head_sha=head_sha,
        outcome=Outcome.COMPLETED,
        findings=findings,
        comment_id=None,
        assessment=assessment,
    )


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield store


@pytest.fixture(name="runs")
def runs_fixture(store):
    return RunStore(store)


@pytest.fixture(name="posted")
def posted_fixture(store):
    """The ids the agent has posted -- what stops it answering itself."""
    return AgentComments(store)


def make_publisher(
    runs, posted, transport, dry_run=False, secrets=(), post_superseded=True
) -> Publisher:
    return Publisher(
        client=GitHubClient(token="t", transport=httpx.MockTransport(transport)),
        endpoints=ENDPOINTS,
        runs=runs,
        posted=posted,
        config=PublishConfig(dry_run=dry_run, post_superseded=post_superseded),
        handle="claude",
        secrets=secrets,
    )


def body_of(transport: Transport) -> str:
    return json.loads(transport.writes[0].content)["body"]


def result_of(findings=FINDINGS, assessment=None):
    return ReviewResult(
        findings=findings,
        usage=Usage(100, UsageConfidence.EXACT, engine="fake"),
        outcome=Outcome.COMPLETED,
        assessment=assessment,
    )


def rendered(findings=NUMBERED, round_number=3, commits=3, handle="claude"):
    return render(
        HEAD,
        findings,
        pr_number=1765,
        round_number=round_number,
        commits=commits,
        handle=handle,
    )
