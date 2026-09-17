"""Daemon loop: what one cycle enqueues, and what it must never enqueue."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pr_review_agent.config import Config
from pr_review_agent.daemon import COMMENTS, EMPTY, PULL_REQUESTS, Daemon, main
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.poller.interval import AdaptiveInterval
from pr_review_agent.poller.poller import Poller
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import SqliteStore

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
RECENT = NOW - timedelta(minutes=5)

ALICE_ID = 7
ALICE = {"id": ALICE_ID, "login": "alice", "type": "User"}

CONFIG = Config.from_mapping(
    {
        "github": {"repo": "o/r", "agent_user_id": 42},
        "triggers": {"allowlist": [ALICE_ID], "handle": "claude"},
    }
)


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


def make_daemon(tmp_path, handler) -> Daemon:
    store = SqliteStore(tmp_path / "state.db")
    poller = Poller(
        client=GitHubClient(token="t", transport=httpx.MockTransport(handler)),
        endpoints=RepoEndpoints("o", "r"),
        etags=store,
        # Zero keeps run_forever's wait instant; run_once ignores it.
        interval=AdaptiveInterval(min_seconds=0, max_seconds=0),
    )
    return Daemon(config=CONFIG, poller=poller, store=store, queue=ReviewQueue(store))


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
    assert daemon.store.watermark(PULL_REQUESTS) == NOW
    assert daemon.store.watermark(COMMENTS) == NOW


async def test_seeding_does_not_rewind_an_existing_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULL_REQUESTS, NOW)
    daemon.seed_watermarks(now=OLD)
    assert daemon.store.watermark(PULL_REQUESTS) == NOW


async def test_a_pull_request_after_the_watermark_is_enqueued(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 1
    assert daemon.queue.status("pr_opened:o/r:3:sha3") is not None


async def test_repolling_the_same_payload_enqueues_once(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

    first = await daemon.run_once()
    # Rewind by hand: the watermark alone would hide the second look, and
    # the dedupe key is what this test is about.
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)
    second = await daemon.run_once()

    assert (first.enqueued, second.enqueued) == (1, 0)
    assert queued(daemon) == 1


async def test_watermark_advances_to_the_newest_item_not_to_now(tmp_path):
    older = RECENT - timedelta(hours=1)
    daemon = make_daemon(
        tmp_path, responder(pulls=[pr_item(3, RECENT), pr_item(4, older)])
    )
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

    await daemon.run_once()

    assert daemon.store.watermark(PULL_REQUESTS) == RECENT


async def test_an_unchanged_endpoint_moves_no_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())  # every endpoint 304s
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)
    daemon.store.advance_watermark(COMMENTS, OLD)

    summary = await daemon.run_once()

    assert summary == EMPTY
    assert daemon.store.watermark(PULL_REQUESTS) == OLD
    assert daemon.store.watermark(COMMENTS) == OLD


async def test_both_comment_endpoints_share_one_watermark(tmp_path):
    daemon = make_daemon(
        tmp_path,
        responder(
            issue_comments=[issue_comment(11, RECENT - timedelta(hours=2))],
            review_comments=[review_comment(12, RECENT)],
        ),
    )
    daemon.store.advance_watermark(COMMENTS, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 2
    assert daemon.store.watermark(COMMENTS) == RECENT


async def test_a_failing_enqueue_leaves_the_watermark_unmoved(tmp_path):
    # Enqueue happens before the watermark advances, so a crash in between
    # costs one re-classification rather than a lost trigger.
    class BrokenQueue(ReviewQueue):
        def enqueue(self, trigger, *, now):
            raise RuntimeError("disk full")

    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.queue = BrokenQueue(daemon.store)
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

    with pytest.raises(RuntimeError):
        await daemon.run_once()

    assert daemon.store.watermark(PULL_REQUESTS) == OLD


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
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

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


def test_main_without_a_token_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert main(["--config", str(tmp_path / "config.yaml")]) == 2
    assert "GITHUB_TOKEN is not set" in capsys.readouterr().err


def test_main_with_an_unreadable_config_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert main(["--config", str(tmp_path / "missing.yaml")]) == 2
    assert "cannot read config" in capsys.readouterr().err
