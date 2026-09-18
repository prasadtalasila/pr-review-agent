"""ReviewWorker: claim through the governor, run, settle, finish.

The whole pipeline, over a `tmp_path` store, the loopback git double and a
`MockTransport` client. No network, no tokens -- which is the point of
building the drainer while the only engine is a fake one.
"""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import httpx
import pytest

from pr_review_agent.budget import Governor, Mode, Usage, UsageConfidence
from pr_review_agent.config import BudgetConfig
from pr_review_agent.engine import FULL, Capabilities, FakeEngine, ReviewRequest
from pr_review_agent.engine.models import ReviewResult
from pr_review_agent.poller.client import GitHubClient, GitHubClientError
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.queue import Claim, QueueStatus, ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import Trigger, TriggerKind
from pr_review_agent.worker import ReviewWorker

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
REPO = "owner/name"
PR = 7
MAX_RUN_TOKENS = 1_000


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


@dataclass
class ExplodingEngine:
    """An engine that fails the way a timed-out CLI adapter would."""

    name: str = "exploding"
    capabilities: Capabilities = FULL
    calls: int = 0

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Fail, having plausibly already spent tokens."""
        self.calls += 1
        raise TimeoutError("the engine did not finish in time")


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
    ledger: list = field(default_factory=list)


def ledger_rows(store: SqliteStore) -> list[tuple]:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT dedupe_key, mode, reserved_tokens, used_tokens, "
            "usage_confidence, engine, model FROM ledger ORDER BY id"
        ).fetchall()


@pytest.fixture(name="wired")
def wired_fixture(tmp_path, workspace, git_remote):
    """A worker over the git double, with a client that answers /pulls/7."""

    def build(engine=None, config=None, client=None, queue_class=ReviewQueue):
        store = SqliteStore(tmp_path / "state.db")
        store.__enter__()
        queue = queue_class(store)
        governor = Governor(store, config or budget())
        worker = ReviewWorker(
            queue=queue,
            governor=governor,
            workspace=workspace,
            engine=engine or FakeEngine(),
            client=client or client_returning(payload(git_remote.head_sha)),
            endpoints=RepoEndpoints("owner", "name"),
            owner="worker-1",
        )
        return Fixture(
            worker=worker,
            store=store,
            queue=queue,
            governor=governor,
            engine=worker.engine,
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
