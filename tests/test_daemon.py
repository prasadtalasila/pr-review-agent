"""Daemon loop: what one cycle enqueues, and what it must never enqueue."""

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import httpx
import pytest

from pr_review_agent._startup import StartupError
from pr_review_agent.budget import Governor
from pr_review_agent.config import Config, GitHubConfig, WorkerConfig
from pr_review_agent.daemon import (
    COMMENTS,
    EMPTY,
    PULL_REQUESTS,
    Daemon,
    build_engine,
    build_workers,
    resolve_budget,
    supervise,
)
from pr_review_agent.engine.claude import ClaudeCliEngine
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.poller.interval import AdaptiveInterval
from pr_review_agent.poller.poller import Poller
from pr_review_agent.publisher import Publisher
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.runs import RunStore
from pr_review_agent.store import SqliteStore
from pr_review_agent.worker import ReviewWorker
from pr_review_agent.workspace import Workspace

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
RECENT = NOW - timedelta(minutes=5)

ALICE_ID = 7
ALICE = {"id": ALICE_ID, "login": "alice", "type": "User"}

BUDGET = {
    "session_tokens": 88_000,
    "weekly_tokens": 1_500_000,
    "max_run_tokens": 60_000,
}

CONFIG = Config.from_mapping(
    {
        "github": {"repo": "o/r"},
        "triggers": {"allowlist": [ALICE_ID], "handle": "claude"},
        "budget": BUDGET,
        "engine": {
            "model": "claude-sonnet-5",
            "expected_version": "2.1.274",
            "timeout_seconds": 900,
        },
    }
)


#: The two watermark keys ``CONFIG``'s repository writes. Qualified by repo,
#: so that several daemons sharing one store for one budget do not overwrite
#: each other's high-water marks.
PULLS_WM = f"{PULL_REQUESTS}:o/r"
COMMENTS_WM = f"{COMMENTS}:o/r"

#: A second repository, for the case one store serves several daemons.
OTHER = replace(CONFIG, github=GitHubConfig(repo="other/repo"))
OTHER_PULLS_WM = f"{PULL_REQUESTS}:other/repo"


def stamp(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def pr_item(number: int, created_at: datetime) -> dict:
    return {
        "number": number,
        "head": {"sha": f"sha{number}"},
        "user": ALICE,
        "created_at": stamp(created_at),
        "draft": False,
    }


def issue_comment(comment_id: int, updated_at: datetime) -> dict:
    return {
        "id": comment_id,
        "user": ALICE,
        "body": "@claude review this",
        "updated_at": stamp(updated_at),
        "issue_url": "https://api.github.com/repos/o/r/issues/12",
        "html_url": "https://github.com/o/r/pull/12#issuecomment-1",
    }


def review_comment(comment_id: int, updated_at: datetime) -> dict:
    return {
        "id": comment_id,
        "user": ALICE,
        "body": "@claude here too",
        "updated_at": stamp(updated_at),
        "pull_request_url": "https://api.github.com/repos/o/r/pulls/12",
    }


def responder(pulls=None, issue_comments=None, review_comments=None):
    """A MockTransport handler: a body means 200, ``None`` means 304."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/pulls/comments" in url:
            body = review_comments
        elif "/issues/comments" in url:
            body = issue_comments
        else:
            body = pulls
        if body is None:
            return httpx.Response(304)
        return httpx.Response(200, json=body, headers={"etag": '"e"'})

    return handler


def make_daemon(tmp_path, handler, config=CONFIG, store=None) -> Daemon:
    # ``store`` is passed in only to put two repositories on one file, which
    # is how a shared budget is deployed.
    store = SqliteStore(tmp_path / "state.db") if store is None else store
    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    endpoints = RepoEndpoints(config.github.owner, config.github.name)
    poller = Poller(
        client=client,
        endpoints=endpoints,
        etags=store,
        # Zero keeps run_forever's wait instant; run_once ignores it.
        interval=AdaptiveInterval(min_seconds=0, max_seconds=0),
    )
    return Daemon(
        config=config,
        poller=poller,
        store=store,
        queue=ReviewQueue(store),
        governor=Governor(store, config.budget),
        publisher=Publisher(
            client=client,
            endpoints=endpoints,
            runs=RunStore(store),
            config=config.publish,
            handle=config.triggers.handle,
        ),
    )


def queued(daemon: Daemon) -> int:
    with daemon.store.transaction() as conn:
        return conn.execute("SELECT count(*) FROM queue").fetchone()[0]


async def test_cold_start_enqueues_nothing_from_the_backlog(tmp_path):
    # The whole point of seeding: a fresh database must not pay to review
    # every open pull request and replay every historical @claude.
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(1, OLD), pr_item(2, OLD + timedelta(days=1))],
            issue_comments=[issue_comment(11, OLD), issue_comment(12, RECENT)],
            review_comments=[review_comment(13, RECENT)],
        ),
    )
    daemon.seed_watermarks(now=NOW)

    summary = await daemon.run_once()

    assert summary.enqueued == 0
    assert queued(daemon) == 0


async def test_seeding_sets_both_watermarks(tmp_path):
    daemon = make_daemon(tmp_path, responder())
    daemon.seed_watermarks(now=NOW)
    assert daemon.store.watermark(PULLS_WM) == NOW
    assert daemon.store.watermark(COMMENTS_WM) == NOW


async def test_seeding_does_not_rewind_an_existing_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULLS_WM, NOW)
    daemon.seed_watermarks(now=OLD)
    assert daemon.store.watermark(PULLS_WM) == NOW


async def test_two_repositories_on_one_store_keep_separate_watermarks(tmp_path):
    # Why the key carries the repository: one store is how several daemons
    # share one budget, and an unqualified key would let whichever polled
    # last overwrite the rest -- every other repository then reading its own
    # backlog as already seen, and skipping it forever.
    store = SqliteStore(tmp_path / "state.db")
    mine = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]), store=store)
    theirs = make_daemon(tmp_path, responder(), config=OTHER, store=store)
    theirs.seed_watermarks(now=NOW)
    mine.store.advance_watermark(PULLS_WM, OLD)

    summary = await mine.run_once()

    assert summary.enqueued == 1
    assert store.watermark(PULLS_WM) == RECENT
    assert store.watermark(OTHER_PULLS_WM) == NOW


async def test_an_unqualified_watermark_is_adopted(tmp_path):
    # A database written before the key carried a repository. Dropping the
    # value re-offers the whole open backlog as new; seeding over it skips
    # every event in flight. Neither is acceptable, so it is carried across.
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULL_REQUESTS, RECENT)
    daemon.store.advance_watermark(COMMENTS, RECENT)

    daemon.seed_watermarks(now=NOW)

    assert daemon.store.watermark(PULLS_WM) == RECENT
    assert daemon.store.watermark(COMMENTS_WM) == RECENT


async def test_an_unqualified_watermark_is_adopted_only_once(tmp_path):
    # The legacy row is left on disk so a downgrade still finds it, which
    # means a later start must ignore it rather than read it again -- here it
    # has moved ahead of the qualified one, so a second adoption would show.
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)
    daemon.seed_watermarks(now=NOW)
    assert daemon.store.watermark(PULLS_WM) == OLD
    daemon.store.advance_watermark(PULL_REQUESTS, NOW + timedelta(days=1))

    daemon.seed_watermarks(now=NOW)

    assert daemon.store.watermark(PULLS_WM) == OLD


async def test_a_pull_request_after_the_watermark_is_enqueued(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULLS_WM, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 1
    assert daemon.queue.status("pr_opened:o/r:3:sha3") is not None


async def test_repolling_the_same_payload_enqueues_once(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULLS_WM, OLD)

    first = await daemon.run_once()
    # Rewind by hand: the watermark alone would hide the second look, and
    # the dedupe key is what this test is about.
    daemon.store.advance_watermark(PULLS_WM, OLD)
    second = await daemon.run_once()

    assert (first.enqueued, second.enqueued) == (1, 0)
    assert queued(daemon) == 1


async def test_watermark_advances_to_the_newest_item_not_to_now(tmp_path):
    older = RECENT - timedelta(hours=1)
    daemon = make_daemon(
        tmp_path, responder(pulls=[pr_item(3, RECENT), pr_item(4, older)])
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)

    await daemon.run_once()

    assert daemon.store.watermark(PULLS_WM) == RECENT


async def test_an_unchanged_endpoint_moves_no_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())  # every endpoint 304s
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    summary = await daemon.run_once()

    assert summary == EMPTY
    assert daemon.store.watermark(PULLS_WM) == OLD
    assert daemon.store.watermark(COMMENTS_WM) == OLD


async def test_both_comment_endpoints_share_one_watermark(tmp_path):
    daemon = make_daemon(
        tmp_path,
        responder(
            issue_comments=[issue_comment(11, RECENT - timedelta(hours=2))],
            review_comments=[review_comment(12, RECENT)],
        ),
    )
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 2
    assert daemon.store.watermark(COMMENTS_WM) == RECENT


# -- comments on closed pull requests -------------------------------------
#
# Both comment endpoints are repo-wide, so they return comments on pull
# requests closed weeks ago. The open set comes off the `/pulls` leg of the
# same sweep and has to survive that leg answering 304.


async def test_a_comment_on_a_closed_pull_request_is_not_enqueued(tmp_path):
    # The comment names pr 12; the only open pull request is 3.
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(3, OLD)],
            issue_comments=[issue_comment(11, RECENT)],
        ),
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 0
    assert queued(daemon) == 0


async def test_a_comment_on_a_pull_request_opened_this_cycle_is_enqueued(tmp_path):
    # Both legs belong to one sweep, and the pulls leg is classified first.
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(12, OLD)],
            issue_comments=[issue_comment(11, RECENT)],
        ),
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    assert (await daemon.run_once()).enqueued == 1


async def test_the_open_pull_request_set_survives_a_304(tmp_path):
    """A 304 on the pulls leg means unchanged, not unknown."""
    cycles = iter(
        [
            # First: pr 12 is open, no comments yet.
            responder(pulls=[pr_item(12, OLD)]),
            # Second: the pulls leg 304s, and the mention arrives.
            responder(issue_comments=[issue_comment(11, RECENT)]),
        ]
    )
    handler = next(cycles)

    def dispatch(request):
        return handler(request)

    daemon = make_daemon(tmp_path, dispatch)
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    await daemon.run_once()
    handler = next(cycles)

    assert (await daemon.run_once()).enqueued == 1


async def test_a_failing_enqueue_leaves_the_watermark_unmoved(tmp_path):
    # Enqueue happens before the watermark advances, so a crash in between
    # costs one re-classification rather than a lost trigger.
    class BrokenQueue(ReviewQueue):
        def enqueue(self, trigger, *, now):
            raise RuntimeError("disk full")

    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.queue = BrokenQueue(daemon.store)
    daemon.store.advance_watermark(PULLS_WM, OLD)

    with pytest.raises(RuntimeError):
        await daemon.run_once()

    assert daemon.store.watermark(PULLS_WM) == OLD


async def test_the_loop_stops_without_waiting_out_the_interval(tmp_path):
    stop = asyncio.Event()
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        polls.append(str(request.url))
        stop.set()
        return httpx.Response(304)

    daemon = make_daemon(tmp_path, handler)
    await asyncio.wait_for(daemon.run_forever(stop), timeout=5)

    assert len(polls) == 3  # exactly one cycle, three endpoints


async def test_a_client_error_does_not_stop_the_loop(tmp_path):
    # A transient network failure must not kill a daemon.
    stop = asyncio.Event()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(500)
        stop.set()
        return httpx.Response(304)

    daemon = make_daemon(tmp_path, handler)
    await asyncio.wait_for(daemon.run_forever(stop), timeout=5)

    assert calls["n"] > 3


async def test_an_unexpected_error_is_not_swallowed(tmp_path):
    # Only GitHubClientError is survivable; a bug must crash loudly.
    class BrokenQueue(ReviewQueue):
        def enqueue(self, trigger, *, now):
            raise RuntimeError("disk full")

    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.queue = BrokenQueue(daemon.store)
    daemon.store.advance_watermark(PULLS_WM, OLD)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(daemon.run_forever(asyncio.Event()), timeout=5)


async def test_an_already_set_stop_runs_no_cycle(tmp_path):
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        polls.append(str(request.url))
        return httpx.Response(304)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(make_daemon(tmp_path, handler).run_forever(stop), timeout=5)

    assert polls == []


# The command-line entry points are exercised in tests/test_cli.py, which
# owns the whole `pr-review-agent <noun> <verb>` tree.


# -- SIGHUP: the kill switch must not need a restart ---------------------


def config_yaml(
    enabled="true", repo="o/r", dry_run="false", authority="true", weekly="1500000"
):
    return (
        f"github:\n  repo: {repo}\n"
        f"triggers:\n  handle: claude\n  allowlist:\n    - {ALICE_ID}\n"
        f"publish:\n  dry_run: {dry_run}\n"
        f"budget:\n  enabled: {enabled}\n"
        f"  authority: {authority}\n"
        "  session_tokens: 88000\n"
        f"  weekly_tokens: {weekly}\n"
        "  max_run_tokens: 60000\n"
        "engine:\n"
        "  model: claude-sonnet-5\n"
        "  expected_version: '2.1.274'\n"
        "  timeout_seconds: 900\n"
    )


def daemon_with_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    daemon = make_daemon(tmp_path, {})
    daemon.config_path = path
    return daemon, path


def test_sighup_reloads_the_kill_switch(tmp_path):
    daemon, path = daemon_with_config(tmp_path, config_yaml(enabled="true"))
    assert daemon.governor.config.enabled is True

    path.write_text(config_yaml(enabled="false"), encoding="utf-8")
    daemon.reload_config()

    assert daemon.governor.config.enabled is False
    assert daemon.config.budget.enabled is False


def test_sighup_with_a_broken_file_keeps_the_previous_config(tmp_path, caplog):
    """A typo must not take the service down -- that is the brake, not a bomb."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(enabled="true"))
    path.write_text("github: [unclosed\n", encoding="utf-8")

    daemon.reload_config()

    assert daemon.governor.config.enabled is True
    assert "keeping the previous configuration" in caplog.text


def test_sighup_reloads_the_quieter_brake(tmp_path):
    """`publish.dry_run` takes the mechanism `budget.enabled` built."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(dry_run="false"))
    assert daemon.publisher.config.dry_run is False

    path.write_text(config_yaml(dry_run="true"), encoding="utf-8")
    daemon.reload_config()

    assert daemon.publisher.config.dry_run is True
    assert daemon.config.publish.dry_run is True


def test_sighup_with_a_broken_file_keeps_the_previous_dry_run(tmp_path):
    """A typo must not silently start posting what a dry run was hiding."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(dry_run="true"))
    daemon.reload_config()
    assert daemon.publisher.config.dry_run is True

    path.write_text("github: [unclosed\n", encoding="utf-8")
    daemon.reload_config()

    assert daemon.publisher.config.dry_run is True


def test_sighup_does_not_swap_a_changed_repository(tmp_path, caplog):
    """Only budget is hot-swapped: the watermarks describe the old repo."""
    daemon, path = daemon_with_config(tmp_path, config_yaml())
    path.write_text(config_yaml(repo="other/repo"), encoding="utf-8")

    daemon.reload_config()

    assert daemon.config.github.repo == "o/r"
    assert "need a restart" in caplog.text


# -- the worker supervisor -----------------------------------------------
#
# A crash must not stop all reviews (the poll loop would keep filling a queue
# nobody drains) and must not spin silently either.


class CrashingWorker:
    """A worker that fails its loop a fixed number of times, then idles."""

    def __init__(self, crashes: int, *, progress_after: int | None = None) -> None:
        self.crashes = crashes
        self.progress_after = progress_after
        self.spawns = 0
        self.completed = 0
        self.owner = "worker-1"

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Crash while there are crashes left, then wait to be stopped."""
        self.spawns += 1
        if self.progress_after is not None and self.spawns > self.progress_after:
            self.completed += 1
        if self.spawns <= self.crashes:
            raise RuntimeError("the worker fell over")
        stop.set()


async def test_a_crashed_worker_is_respawned(tmp_path, monkeypatch):
    monkeypatch.setattr("pr_review_agent.daemon.RESPAWN_BACKOFF", 0.01)
    worker = CrashingWorker(crashes=2)
    stop = asyncio.Event()

    await asyncio.wait_for(supervise(cast(ReviewWorker, worker), stop), timeout=5)

    assert worker.spawns == 3


async def test_a_crash_does_not_propagate_to_the_daemon(tmp_path, monkeypatch):
    """Killing the process would take the poller down with the worker."""
    monkeypatch.setattr("pr_review_agent.daemon.RESPAWN_BACKOFF", 0.01)
    worker = CrashingWorker(crashes=1)

    await asyncio.wait_for(
        supervise(cast(ReviewWorker, worker), asyncio.Event()), timeout=5
    )


async def test_the_backoff_doubles_while_the_worker_makes_no_progress(monkeypatch):
    waits = []

    async def record(_stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr("pr_review_agent.daemon.RESPAWN_BACKOFF", 1.0)
    monkeypatch.setattr("pr_review_agent.daemon._wait", record)
    worker = CrashingWorker(crashes=3)

    await asyncio.wait_for(
        supervise(cast(ReviewWorker, worker), asyncio.Event()), timeout=5
    )

    assert waits == [1.0, 2.0, 4.0]


async def test_a_worker_that_reviewed_something_starts_over_at_the_floor(monkeypatch):
    """Progress means the fault was not persistent; the delay is not earned."""
    waits = []

    async def record(_stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr("pr_review_agent.daemon.RESPAWN_BACKOFF", 1.0)
    monkeypatch.setattr("pr_review_agent.daemon._wait", record)
    worker = CrashingWorker(crashes=3, progress_after=2)

    await asyncio.wait_for(
        supervise(cast(ReviewWorker, worker), asyncio.Event()), timeout=5
    )

    assert waits == [1.0, 2.0, 1.0]


async def test_the_backoff_is_capped(monkeypatch):
    waits = []

    async def record(_stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr("pr_review_agent.daemon.RESPAWN_BACKOFF", 1.0)
    monkeypatch.setattr("pr_review_agent.daemon.RESPAWN_BACKOFF_MAX", 2.0)
    monkeypatch.setattr("pr_review_agent.daemon._wait", record)
    worker = CrashingWorker(crashes=4)

    await asyncio.wait_for(
        supervise(cast(ReviewWorker, worker), asyncio.Event()), timeout=5
    )

    assert waits == [1.0, 2.0, 2.0, 2.0]


async def test_a_stopped_supervisor_does_not_respawn(monkeypatch):
    monkeypatch.setattr("pr_review_agent.daemon.RESPAWN_BACKOFF", 0.01)
    worker = CrashingWorker(crashes=100)
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(supervise(cast(ReviewWorker, worker), stop), timeout=5)

    assert worker.spawns == 0


# -- wiring: how many workers, and who they are --------------------------


def make_workers(tmp_path, count):
    daemon = make_daemon(tmp_path, lambda _request: httpx.Response(304))
    daemon.config = replace(CONFIG, worker=WorkerConfig(count=count))
    return build_workers(
        daemon,
        workspace=Workspace("o/r", tmp_path / "cache"),
        engine=build_engine(CONFIG),
        client=GitHubClient(token="t"),
        endpoints=RepoEndpoints("o", "r"),
    )


def test_one_worker_is_built_by_default(tmp_path):
    assert len(make_workers(tmp_path, 1)) == 1


def test_the_configured_number_of_workers_is_built(tmp_path):
    assert len(make_workers(tmp_path, 3)) == 3


def test_every_worker_owns_its_claims_distinctly(tmp_path):
    """The lease guard is on the owner, so two workers sharing one is a bug."""
    owners = [worker.owner for worker in make_workers(tmp_path, 4)]
    assert len(set(owners)) == 4


def test_workers_share_the_queue_and_the_governor(tmp_path):
    """One ledger, one queue: separate governors would each see their own."""
    workers = make_workers(tmp_path, 3)
    assert len({id(worker.governor) for worker in workers}) == 1
    assert len({id(worker.queue) for worker in workers}) == 1


def test_workers_share_one_publisher(tmp_path):
    """A second publisher would be a second thing for SIGHUP to find."""
    workers = make_workers(tmp_path, 3)
    assert len({id(worker.publisher) for worker in workers}) == 1


def test_every_worker_can_record_a_run(tmp_path):
    """One SQLite file, so a RunStore each is a view rather than a copy."""
    workers = make_workers(tmp_path, 3)
    assert all(worker.runs is not None for worker in workers)


# -- which engine the daemon runs ----------------------------------------


def test_the_configured_engine_is_what_the_workers_run():
    """The seam has a real caller now: this is what makes the agent spend."""
    engine = build_engine(CONFIG)
    assert isinstance(engine, ClaudeCliEngine)
    assert engine.model == "claude-sonnet-5"
    assert engine.expected_version == "2.1.274"
    assert engine.timeout_seconds == 900


def test_the_engine_carries_the_configured_binary_and_standards():
    config = replace(
        CONFIG,
        engine=replace(
            CONFIG.engine, binary="/opt/claude", standards_paths=("AGENTS.md",)
        ),
    )
    engine = build_engine(config)
    assert engine.binary == "/opt/claude"
    assert engine.standards_paths == ("AGENTS.md",)


def test_no_fake_engine_reaches_a_running_daemon(tmp_path):
    """FakeEngine is a test double; a daemon running one would review nothing."""
    workers = make_workers(tmp_path, 2)
    assert all(isinstance(w.engine, ClaudeCliEngine) for w in workers)


# -- the shared budget policy: whose numbers govern one store ------------


def with_budget(config=CONFIG, **overrides):
    return replace(config, budget=replace(config.budget, **overrides))


def test_an_authority_publishes_the_pool_arithmetic(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        comply = resolve_budget(store, with_budget(authority=True), now=NOW)
        published = store.budget_policy()

    assert comply is False
    assert published is not None
    assert published.authority_repo == "o/r"
    assert published.fields == CONFIG.budget.shared()


def test_a_lone_daemon_publishes_without_being_told_to(tmp_path):
    """Both keys default true, so a single deployment needs neither."""
    with SqliteStore(tmp_path / "state.db") as store:
        assert resolve_budget(store, CONFIG, now=NOW) is False
        assert store.budget_policy() is not None


def test_a_complier_refuses_to_start_before_any_authority(tmp_path):
    # Retryable by design: the unit restarts on failure, so a complier
    # started before its authority waits rather than needing a start order.
    with (
        SqliteStore(tmp_path / "state.db") as store,
        pytest.raises(StartupError, match="no authority has published"),
    ):
        resolve_budget(store, with_budget(authority=False), now=NOW)


def test_a_complier_starts_once_the_authority_has_published(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, OTHER, now=NOW)

        assert resolve_budget(store, with_budget(authority=False), now=NOW) is True


def test_a_second_authority_refuses_to_start(tmp_path):
    """Two authorities are two opinions about one allowance."""
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, with_budget(OTHER, authority=True), now=NOW)

        with pytest.raises(StartupError, match="already the budget authority"):
            resolve_budget(store, with_budget(authority=True), now=NOW)


def test_an_authority_republishes_on_restart(tmp_path):
    """Its file is the declared truth; the row is only ever a copy of it."""
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, with_budget(authority=True), now=NOW)

        resolve_budget(
            store, with_budget(authority=True, weekly_tokens=3_000_000), now=NOW
        )

        published = store.budget_policy()
        assert published is not None
        assert published.fields["weekly_tokens"] == 3_000_000


def test_declining_to_comply_beside_an_authority_warns(tmp_path, caplog):
    """The one remaining way to overspend a shared pool, so it is said loudly."""
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, with_budget(OTHER, authority=True), now=NOW)

        declined = with_budget(authority=False, comply=False)
        assert resolve_budget(store, declined, now=NOW) is False

    assert "budget.comply is false" in caplog.text


def test_sighup_republishes_an_authoritys_policy(tmp_path):
    """An authority's reload is how a shared limit changes for everyone."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(authority="true"))
    path.write_text(config_yaml(authority="true", weekly="3000000"), encoding="utf-8")

    daemon.reload_config()

    published = daemon.store.budget_policy()
    assert published is not None
    assert published.fields["weekly_tokens"] == 3_000_000


def test_sighup_does_not_claim_a_compliers_limits_changed(tmp_path, caplog):
    """A complier's own token counts are inert, so the log must not imply
    a reload applied them."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(authority="false"))
    path.write_text(config_yaml(authority="false", weekly="3000000"), encoding="utf-8")

    with caplog.at_level(logging.INFO):
        daemon.reload_config()

    assert "the limits in force remain the authority's" in caplog.text
