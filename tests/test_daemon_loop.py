"""The process around the cycle: run_forever, run_together, supervise.

A transient failure must not kill a daemon and a crashed worker must not
take the loop with it, but an unexpected error has to be loud -- a daemon
that polls happily while enqueueing nothing looks healthy and reviews
nothing.
"""

import asyncio
import sqlite3
from typing import cast

import httpx
import pytest
from daemon_harness import (
    OLD,
    PULLS_WM,
    RECENT,
    make_daemon,
    pr_item,
    queued,
    responder,
)

from pr_review_agent.daemon import run_together, supervise
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import SqliteStore
from pr_review_agent.worker import ReviewWorker


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
    daemon.queue = BrokenQueue(daemon.store, repo=daemon.config.github.repo)
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


async def test_store_contention_does_not_stop_the_loop(tmp_path):
    # Several daemons on one file can exceed the 5 s busy_timeout. That says
    # nothing about this process and is over by the next cycle, so restarting
    # for it throws away a poller that was working.
    stop = asyncio.Event()
    calls = {"n": 0}

    class BusyQueue(ReviewQueue):
        def enqueue(self, trigger, *, now):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            stop.set()
            return super().enqueue(trigger, now=now)

    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.queue = BusyQueue(daemon.store, repo=daemon.config.github.repo)
    daemon.store.advance_watermark(PULLS_WM, OLD)

    await asyncio.wait_for(daemon.run_forever(stop), timeout=5)

    assert calls["n"] == 3
    assert queued(daemon) == 1


async def test_a_store_error_that_is_not_contention_still_crashes(tmp_path):
    # The widened catch must not turn a schema bug into a silent spin.
    class BrokenQueue(ReviewQueue):
        def enqueue(self, trigger, *, now):
            raise sqlite3.OperationalError("no such table: queue")

    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.queue = BrokenQueue(daemon.store, repo=daemon.config.github.repo)
    daemon.store.advance_watermark(PULLS_WM, OLD)

    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(daemon.run_forever(asyncio.Event()), timeout=5)


async def test_a_failing_task_stops_the_others_before_the_error_escapes():
    stop = asyncio.Event()
    order = []

    async def poller():
        raise RuntimeError("poll cycle failed")

    async def worker():
        await stop.wait()
        order.append("worker unwound")

    with pytest.raises(RuntimeError, match="poll cycle failed"):
        await asyncio.wait_for(run_together(stop, poller(), worker()), timeout=5)

    assert order == ["worker unwound"]
    assert stop.is_set()


async def test_the_store_is_still_open_while_the_worker_unwinds(tmp_path):
    # The regression `asyncio.gather` caused: the error left `run`'s `with
    # SqliteStore(...)` block while a worker was still mid-review, so its
    # teardown ran against a closed connection and logged a second,
    # misleading traceback over the first.
    stop = asyncio.Event()
    store = SqliteStore(tmp_path / "state.db")
    seen = []

    async def poller():
        raise RuntimeError("poll cycle failed")

    async def worker():
        await stop.wait()
        seen.append(store.watermark(PULLS_WM))

    with pytest.raises(RuntimeError), store:
        await asyncio.wait_for(run_together(stop, poller(), worker()), timeout=5)

    assert seen == [None]  # read, not `ProgrammingError: closed database`


async def test_a_task_that_ignores_stop_is_cancelled_after_the_grace():
    # The grace is for the review already being paid for, not an indefinite
    # wait: what is still running when it lapses is cancelled.
    stop = asyncio.Event()
    cancelled = asyncio.Event()

    async def poller():
        raise RuntimeError("poll cycle failed")

    async def deaf():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(RuntimeError, match="poll cycle failed"):
        await asyncio.wait_for(run_together(stop, poller(), deaf(), grace=0), timeout=5)

    assert cancelled.is_set()


async def test_run_together_returns_when_every_task_finishes():
    stop = asyncio.Event()
    finished = []

    async def one():
        finished.append(1)

    async def two():
        finished.append(2)

    await asyncio.wait_for(run_together(stop, one(), two()), timeout=5)

    assert sorted(finished) == [1, 2]
    assert not stop.is_set()


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
    monkeypatch.setattr("pr_review_agent.daemon.wait_until", record)
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
    monkeypatch.setattr("pr_review_agent.daemon.wait_until", record)
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
    monkeypatch.setattr("pr_review_agent.daemon.wait_until", record)
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
