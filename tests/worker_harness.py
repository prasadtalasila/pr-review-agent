"""The worker suite's doubles: a fake GitHub, four engines, and one fixture.

Every `test_worker_*.py` module drives `ReviewWorker` through the wiring
built here -- a `tmp_path` store, the loopback git double and a
`MockTransport` client -- so the doubles live in one place rather than in
whichever module happened to need them first.

`POSIX_ONLY` travels with them: every test in the family drives a real
checkout, so the whole family skips where the git tests do.
"""

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pytest

from pr_review_agent.budget import (
    Governor,
    Usage,
    UsageConfidence,
)
from pr_review_agent.comments import AgentComments
from pr_review_agent.config import (
    DEFAULT_MAX_PUBLISH_ATTEMPTS,
    BudgetConfig,
    PublishConfig,
)
from pr_review_agent.engine import (
    FULL,
    Capabilities,
    EngineTimeout,
    EngineUnavailable,
    FakeEngine,
    ReviewRequest,
)
from pr_review_agent.engine.models import Outcome, ReviewResult
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.publisher import Publisher
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.runs import RunStore
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import CommentSource, Trigger, TriggerKind
from pr_review_agent.worker import ReviewWorker

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
REPO = "owner/name"
PR = 7
MAX_RUN_TOKENS = 1_000

# Every test here drives a real checkout, so this file skips where the git
# tests do: the daemon is deployed on POSIX, and Git for Windows differs.
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)


def budget(**overrides) -> BudgetConfig:
    # The pacer is off unless a test is about it. These cases review one
    # pull request several times inside a few simulated seconds, which is
    # exactly what the shipped interval defers -- and what it defers is
    # `tests/test_pacing.py`'s subject, not theirs.
    base = {
        "session_tokens": 100_000,
        "weekly_tokens": 1_000_000,
        "max_run_tokens": MAX_RUN_TOKENS,
        "min_review_interval_seconds": 0,
        "mention_min_review_interval_seconds": 0,
    }
    return BudgetConfig(**{**base, **overrides})


def opened(head_sha="abc123") -> Trigger:
    return Trigger(
        kind=TriggerKind.PR_OPENED,
        repo=REPO,
        pr_number=PR,
        head_sha=head_sha,
        actor_id=114395272,
        dedupe_key=f"pr_opened:{REPO}:{PR}:{head_sha}",
    )


def mention(comment_id=99) -> Trigger:
    return Trigger(
        kind=TriggerKind.MENTION,
        repo=REPO,
        pr_number=PR,
        head_sha=None,
        actor_id=114395272,
        dedupe_key=f"mention:{REPO}:{PR}:{comment_id}",
        comment_id=comment_id,
        comment_source=CommentSource.ISSUE,
    )


def payload(head_sha: str, **overrides) -> dict:
    return {
        "number": PR,
        "head": {"sha": head_sha},
        "base": {"ref": "main"},
        "additions": 2,
        "deletions": 0,
        "changed_files": 1,
        "state": "open",
        **overrides,
    }


def client_returning(body: dict | None, status: int = 200) -> GitHubClient:
    """A real client over a mock transport: no socket, no token."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return GitHubClient(token="fake-token", transport=httpx.MockTransport(handler))


class GitHubDouble:
    """Answers reads with the pull request and writes with a comment id.

    Records every request, because half of what the publisher has to get
    right is *which* call it made and in what order.
    """

    def __init__(
        self,
        head_sha: str,
        comment_id: int = 555,
        write_status: int = 201,
        head_moves_to: str | None = None,
        pull_state: str = "open",
        merged: bool = False,
    ):
        self.requests: list[httpx.Request] = []
        self._head_sha = head_sha
        self._comment_id = comment_id
        self._write_status = write_status
        # The worker and the publisher read the same endpoint minutes apart,
        # so a superseded head is a head that changes *between* those two
        # reads -- not one that was always wrong.
        self._head_moves_to = head_moves_to
        # What `/pulls/{n}` says the pull request's state is, which is the
        # only thing standing between a claim and a paid review of
        # something nobody can read any more.
        self._pull_state = pull_state
        self._merged = merged
        self._reads = 0

    def client(self) -> GitHubClient:
        return GitHubClient(
            token="fake-token", transport=httpx.MockTransport(self._handle)
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            self._reads += 1
            head = self._head_sha
            if self._head_moves_to is not None and self._reads > 1:
                head = self._head_moves_to
            return httpx.Response(
                200,
                json=payload(head, state=self._pull_state, merged=self._merged),
            )
        if self._write_status >= 400:
            return httpx.Response(self._write_status, text="nope")
        return httpx.Response(self._write_status, json={"id": self._comment_id})

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    @property
    def comments(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/comments" in r.url.path]

    @property
    def reactions(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/reactions")]


@dataclass
class ExplodingEngine:
    """An engine that falls over mid-run, having plausibly already spent.

    It raises a bare ``TimeoutError`` rather than ``EngineTimeout``: the
    adapter did not report its own wall clock, so the worker cannot tell this
    from any other way a foreign tool can die. ``TimingOutEngine`` is the one
    that did report it.
    """

    name: str = "exploding"
    capabilities: Capabilities = FULL
    calls: int = 0

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Fail, having plausibly already spent tokens."""
        self.calls += 1
        raise TimeoutError("the engine did not finish in time")


@dataclass
class UnstartableEngine:
    """An adapter whose subprocess never came into being.

    What ``CliEngine._start`` raises when ``create_subprocess_exec`` fails --
    a missing binary, or a cwd that is not there. No process existed, so the
    spend is zero and that is provable rather than assumed.
    """

    name: str = "unstartable"
    capabilities: Capabilities = FULL

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Fail before any process exists."""
        del request
        raise EngineUnavailable("cannot run 'claude' in /nowhere: [Errno 2]")


@dataclass
class TimingOutEngine:
    """An engine killed by its wall clock, as ``CliEngine.run`` kills one."""

    name: str = "timing-out"
    capabilities: Capabilities = FULL
    calls: int = 0

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Outlive the clock, having plausibly already spent tokens."""
        self.calls += 1
        raise EngineTimeout("timing-out exceeded 900.0s and was killed")


@dataclass
class SpyQueue(ReviewQueue):
    """A queue that records the admit hook it was handed."""

    def __init__(self, store: SqliteStore, *, repo: str = REPO) -> None:
        super().__init__(store, repo=repo)
        self.admits: list[object] = []

    def claim(self, *, now, owner, admit=None):
        """Record the hook, then claim normally."""
        self.admits.append(admit)
        return super().claim(now=now, owner=owner, admit=admit)


@dataclass
class Fixture:
    """Everything one worker needs, wired together."""

    worker: ReviewWorker
    store: SqliteStore
    queue: ReviewQueue
    governor: Governor
    engine: object
    runs: RunStore
    github: GitHubDouble | None = None
    ledger: list = field(default_factory=list)


def ledger_rows(store: SqliteStore) -> list[tuple]:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT dedupe_key, mode, reserved_tokens, used_tokens, "
            "usage_confidence, engine, model FROM ledger ORDER BY id"
        ).fetchall()


def posted_comment(store: SqliteStore, key: str) -> int | None:
    """The comment id a run recorded, now that nothing reads it back."""
    with store.transaction() as conn:
        return conn.execute(
            "SELECT comment_id FROM runs WHERE dedupe_key = :key", {"key": key}
        ).fetchone()[0]


def publish_state(store: SqliteStore, key: str) -> tuple:
    """How many posts were tried for a run, and whether it was given up on."""
    with store.transaction() as conn:
        return conn.execute(
            "SELECT publish_attempts, publish_failed_at FROM runs "
            "WHERE dedupe_key = :key",
            {"key": key},
        ).fetchone()


def reviewed_lines(store: SqliteStore) -> list[int | None]:
    with store.transaction() as conn:
        return [
            row[0]
            for row in conn.execute("SELECT reviewed_lines FROM ledger ORDER BY id")
        ]


def stop_reasons(store: SqliteStore) -> list[str]:
    with store.transaction() as conn:
        return [
            row[0] for row in conn.execute("SELECT stop_reason FROM ledger ORDER BY id")
        ]


@pytest.fixture(name="wired")
def wired_fixture(tmp_path, workspace, git_remote):
    """A worker over the git double, with a client that answers /pulls/7."""

    def build(
        engine=None,
        config=None,
        client=None,
        queue_class=ReviewQueue,
        dry_run=False,
        github=None,
        max_publish_attempts=DEFAULT_MAX_PUBLISH_ATTEMPTS,
    ):
        store = SqliteStore(tmp_path / "state.db")
        store.__enter__()
        queue = queue_class(store, repo=REPO)
        governor = Governor(store, config or budget())
        runs = RunStore(store)
        endpoints = RepoEndpoints("owner", "name")
        if client is None and github is None:
            github = GitHubDouble(git_remote.head_sha)
        resolved = github.client() if client is None and github else client
        assert resolved is not None
        worker = ReviewWorker(
            queue=queue,
            governor=governor,
            workspace=workspace,
            engine=engine or FakeEngine(),
            client=resolved,
            endpoints=endpoints,
            publisher=Publisher(
                client=resolved,
                endpoints=endpoints,
                runs=runs,
                posted=AgentComments(store),
                config=PublishConfig(
                    dry_run=dry_run, max_publish_attempts=max_publish_attempts
                ),
                handle="claude",
            ),
            runs=runs,
            owner="worker-1",
        )
        return Fixture(
            worker=worker,
            store=store,
            queue=queue,
            governor=governor,
            engine=worker.engine,
            runs=runs,
            github=github,
        )

    built: list[Fixture] = []

    def factory(**kwargs):
        fixture = build(**kwargs)
        built.append(fixture)
        return fixture

    yield factory
    for fixture in built:
        fixture.store.__exit__(None, None, None)


def recorded_result():
    """A completed review, as the engine would have returned it."""
    return ReviewResult(
        findings=(),
        usage=Usage(1_000, UsageConfidence.EXACT, engine="fake", model="fake-1"),
        outcome=Outcome.COMPLETED,
    )


def result_of(outcome, tokens=500):
    return ReviewResult(
        findings=(),
        usage=Usage(tokens, UsageConfidence.EXACT, engine="fake", model="fake-1"),
        outcome=outcome,
    )


@dataclass
class OutcomeEngine(FakeEngine):
    """An engine that ends a run the way the adapter's envelope says it did."""

    outcome: Outcome = Outcome.COMPLETED

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Answer with the configured outcome, and a real token count."""
        self.requests.append(request)
        return result_of(self.outcome)
