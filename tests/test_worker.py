"""ReviewWorker: claim through the governor, run, settle, finish.

The whole pipeline, over a `tmp_path` store, the loopback git double and a
`MockTransport` client. No network, no tokens -- which is the point of
building the drainer while the only engine is a fake one.
"""

import asyncio
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import httpx
import pytest

from pr_review_agent.budget import Governor, Mode, StopReason, Usage, UsageConfidence
from pr_review_agent.config import BudgetConfig, PublishConfig
from pr_review_agent.engine import (
    FULL,
    Capabilities,
    EngineTimeout,
    FakeEngine,
    ReviewRequest,
)
from pr_review_agent.engine.models import Outcome, ReviewResult
from pr_review_agent.poller.client import GitHubClient, GitHubClientError
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.publisher import Publisher
from pr_review_agent.queue import Claim, QueueStatus, ReviewQueue
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
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)


def budget(**overrides) -> BudgetConfig:
    base = {
        "session_tokens": 100_000,
        "weekly_tokens": 1_000_000,
        "max_run_tokens": MAX_RUN_TOKENS,
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
    ):
        self.requests: list[httpx.Request] = []
        self._head_sha = head_sha
        self._comment_id = comment_id
        self._write_status = write_status
        # The worker and the publisher read the same endpoint minutes apart,
        # so a superseded head is a head that changes *between* those two
        # reads -- not one that was always wrong.
        self._head_moves_to = head_moves_to
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
            return httpx.Response(200, json=payload(head))
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

    def __init__(self, store: SqliteStore) -> None:
        super().__init__(store)
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
    ):
        store = SqliteStore(tmp_path / "state.db")
        store.__enter__()
        queue = queue_class(store)
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
                config=PublishConfig(dry_run=dry_run),
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


# -- the spending rail ---------------------------------------------------


async def test_a_claim_is_taken_only_through_the_governor(wired):
    """CLAUDE.md section 5, pinned by a test rather than held by review."""
    fixture = wired(queue_class=SpyQueue)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert isinstance(fixture.queue, SpyQueue)
    assert fixture.queue.admits == [fixture.governor.admit]


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


async def test_the_engine_is_handed_the_checkout_and_the_rung(wired, git_remote):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    (request,) = engine.requests
    assert request.mode is Mode.FULL
    assert request.trigger.dedupe_key == opened().dedupe_key
    assert request.checkout.head_sha == git_remote.head_sha
    assert "feature.py" in request.checkout.diff
    assert request.facts.number == PR


async def test_a_mention_is_reviewed_at_the_head_the_api_reports(wired, git_remote):
    """A mention's payload carries no head_sha; the facts read resolves it."""
    fixture = wired()
    fixture.queue.enqueue(mention(), now=NOW)

    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    (request,) = engine.requests
    assert request.trigger.head_sha is None
    assert request.checkout.head_sha == git_remote.head_sha


async def test_the_checkout_is_gone_once_the_review_is_over(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    (request,) = engine.requests
    assert not request.checkout.path.exists()


async def test_nothing_carries_from_one_run_to_the_next(wired):
    """Two runs, two trees. The tree under review is untrusted input."""
    fixture = wired()
    fixture.queue.enqueue(opened(head_sha="aaa"), now=NOW)
    fixture.queue.enqueue(opened(head_sha="bbb"), now=NOW)

    await fixture.worker.run_once()
    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    first, second = engine.requests
    assert first.checkout.path != second.checkout.path
    assert not first.checkout.path.exists()


# -- failure: what it settles at, and what it leaves behind --------------


async def test_an_engine_failure_settles_the_full_reservation(wired):
    """Anything may have been spent, and the governor cannot find out."""
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, engine, _ = row
    assert used == reserved == MAX_RUN_TOKENS
    assert confidence == str(UsageConfidence.UNAVAILABLE)
    assert engine == "exploding"


async def test_an_engine_failure_is_retried(wired):
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING


async def test_a_failure_before_the_engine_settles_nothing(wired):
    """Nothing reached an engine: that is provable, not assumed."""
    fixture = wired(client=client_returning(None, status=500))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, _, _ = row
    assert reserved == MAX_RUN_TOKENS
    assert (used, confidence) == (0, str(UsageConfidence.UNAVAILABLE))


async def test_a_client_failure_is_retried_with_its_attempt_spent(wired):
    fixture = wired(client=client_returning(None, status=500))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING
    again = fixture.queue.claim(now=NOW, owner="w2")
    assert again is not None and again.attempts == 2


async def test_an_oversized_pull_request_is_abandoned(wired):
    """Deterministic on this head: two more attempts reach the same refusal."""
    fixture = wired(config=budget(max_changed_files=0))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED
    (row,) = ledger_rows(fixture.store)
    assert row[3] == 0


async def test_an_unusable_payload_is_abandoned(wired):
    fixture = wired(client=client_returning({"number": PR}))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


async def test_an_abandoned_trigger_is_never_offered_again(wired):
    fixture = wired(config=budget(max_changed_files=0))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.claim(now=NOW, owner="w2") is None


# -- the lapsed lease ----------------------------------------------------


async def test_a_lapsed_worker_discards_its_run_before_starting(wired):
    """No reservation is held for it, so there is nothing to spend."""
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    assert claim is not None

    await fixture.worker.run_one(replace(claim, owner="somebody-else"))

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    assert engine.requests == []
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.CLAIMED


async def test_a_lapsed_worker_settles_nothing(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    assert claim is not None

    await fixture.worker.run_one(replace(claim, owner="somebody-else"))

    (row,) = ledger_rows(fixture.store)
    assert row[3] is None  # used_tokens: still unsettled, still charged


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


# -- Outcome decides what the row becomes --------------------------------


def _result(outcome, tokens=500):
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
        return _result(self.outcome)


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


# -- the publisher's two call sites --------------------------------------
#
# The acknowledgement is immediate and the publication is late, and the
# order between them is what the 15 s criterion rests on.


async def test_the_trigger_is_acknowledged_before_anything_slow(wired):
    """A review takes minutes; the 👀 must not queue behind it."""
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.paths[0].endswith("/reactions")


async def test_a_mention_is_acknowledged_on_its_own_comment(wired):
    fixture = wired()
    fixture.queue.enqueue(mention(comment_id=4321), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.reactions[0].url.path == (
        "/repos/owner/name/issues/comments/4321/reactions"
    )


async def test_a_failed_acknowledgement_still_yields_a_review(wired, git_remote):
    """Losing a courtesy must not cost a reserved review."""
    github = GitHubDouble(git_remote.head_sha, write_status=500)
    fixture = wired(github=github)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.engine.requests  # the engine still ran
    (row,) = ledger_rows(fixture.store)
    assert row[3] == 1_000  # and it still settled what it spent


async def test_a_completed_review_is_recorded_then_published(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.comments[0].method == "POST"
    assert fixture.runs.comment_for_pull_request(REPO, PR) == 555
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE


async def test_a_superseded_head_is_never_posted(wired, git_remote):
    """The review ran against a head the pull request has since left.

    The head moves between the worker's read and the publisher's, which is
    the only way it can move: both read the same endpoint.
    """
    github_moving = GitHubDouble(
        git_remote.head_sha, head_moves_to="a-newer-commit-entirely"
    )
    fixture = wired(github=github_moving)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.comments == []
    # Done rather than retried: a push is not a trigger, so another attempt
    # would re-read the same stale sha and reserve allowance to do it.
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE


async def test_a_failed_publish_is_retried_without_a_second_review(wired, git_remote):
    """The money is already spent; a flaky write must not spend it again."""
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING
    assert len(fixture.engine.requests) == 1

    github._write_status = 201  # GitHub recovers
    await fixture.worker.run_once()

    assert len(fixture.engine.requests) == 1  # the engine was not run again
    assert fixture.runs.comment_for_pull_request(REPO, PR) == 555
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE


async def test_a_republished_run_costs_nothing(wired, git_remote):
    """A resume reaches no engine, and its ledger row has to say so."""
    github = GitHubDouble(git_remote.head_sha, write_status=502)
    fixture = wired(github=github)
    fixture.queue.enqueue(opened(), now=NOW)
    await fixture.worker.run_once()

    github._write_status = 201
    await fixture.worker.run_once()

    resumed = ledger_rows(fixture.store)[-1]
    assert resumed[3] == 0  # used_tokens
    assert resumed[4] == str(UsageConfidence.EXACT)


async def test_a_lapsed_lease_publishes_nothing(wired):
    """`settle` returning False is how a worker learns to discard a result.

    It now discards a comment under the agent's own account, not just a
    number.
    """
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    stale = replace(claim, owner="a-worker-that-died")

    await fixture.worker.run_one(stale)

    assert fixture.github.comments == []


@pytest.mark.parametrize("outcome", [Outcome.TRUNCATED, Outcome.FAILED])
async def test_an_unfinished_run_publishes_nothing(wired, outcome):
    """Only a completed run carries publishable findings."""
    fixture = wired(engine=OutcomeEngine(outcome=outcome))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github.comments == []
    assert fixture.runs.unpublished_for(REPO, PR) is None


async def test_a_dry_run_reviews_and_posts_nothing(wired):
    """The full pipeline, spending the same tokens, with no comment."""
    fixture = wired(dry_run=True)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.engine.requests
    assert fixture.github.comments == []
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE


async def test_a_dry_run_is_not_re_offered_forever(wired):
    fixture = wired(dry_run=True)
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.runs.unpublished_for(REPO, PR) is None
