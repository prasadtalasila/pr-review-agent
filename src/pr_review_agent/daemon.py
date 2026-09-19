"""Run the poller on a schedule and turn what it sees into queued work.

This is the wiring between two halves that already exist. The poller knows
what changed, the classifier knows what may be reviewed, and the queue knows
what has already been paid for. The loop **stops at ``enqueue``**: it claims
nothing and calls no review engine, so it spends no tokens. A claim is the
point at which work becomes expensive, and guarding it is the budget
governor's job.

Three rules carry the correctness, and each one exists to stop allowance
being spent on work that was already decided.

**Cold start is the spend bound.** A watermark that has never been set is
seeded to the moment the daemon started, so nothing pre-dating the first
start is ever enqueued. Without it, a fresh database re-offers the entire
open backlog as new pull requests and replays every historical ``@claude``
as a new request.

**A watermark advances to the newest timestamp seen in the payload**, never
to wall-clock now. An item that exists but is not yet visible to the API
would otherwise fall into the gap between the two and be skipped forever.

**A watermark advances only after the enqueue.** A crash in between costs one
re-classification, which ``INSERT OR IGNORE`` makes free; the reverse order
loses the trigger permanently.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ._startup import StartupError, startup
from .budget import Governor
from .config import Config, ConfigError
from .engine import ReviewEngine
from .engine.claude import ClaudeCliEngine
from .poller import payloads
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import Endpoint, RepoEndpoints
from .poller.poller import PollCycle, Poller
from .publisher import Publisher
from .queue import ReviewQueue
from .runs import RunStore
from .store import SqliteStore
from .triggers.models import Decision
from .worker import ReviewWorker
from .workspace import Workspace

logger = logging.getLogger(__name__)

TOKEN_ENV = "GITHUB_TOKEN"

#: The two watermark names STORAGE.md declares. Both comment endpoints feed
#: ``COMMENTS``: GitHub's ``updated`` only ever moves forward, so a single
#: high-water mark cannot hide a comment that surfaces later on the other.
PULL_REQUESTS = "pull_requests"
COMMENTS = "comments"

COMMENT_ENDPOINTS = (Endpoint.ISSUE_COMMENTS, Endpoint.REVIEW_COMMENTS)

#: How long the supervisor waits before restarting a worker that fell
#: over, and the ceiling that delay doubles towards. Constants rather
#: than configuration: a backoff describes a failure mode, not an
#: operator's preference, and nobody could set one from outside.
RESPAWN_BACKOFF = 5.0
RESPAWN_BACKOFF_MAX = 300.0


@dataclass(frozen=True)
class CycleSummary:
    """How much one cycle looked at, and how much of it was new work."""

    seen: int
    enqueued: int

    def __add__(self, other: CycleSummary) -> CycleSummary:
        """Combine the two halves of a cycle, or a cycle and one item."""
        return CycleSummary(
            seen=self.seen + other.seen,
            enqueued=self.enqueued + other.enqueued,
        )


#: A cycle that looked at nothing -- every endpoint answered 304.
EMPTY = CycleSummary(seen=0, enqueued=0)


@dataclass
class Daemon:
    """One repository's poll-classify-enqueue loop."""

    config: Config
    poller: Poller
    store: SqliteStore
    queue: ReviewQueue
    governor: Governor
    publisher: Publisher
    config_path: Path | None = None

    #: The pull requests the last ``/pulls?state=open`` payload named, used
    #: to drop comments on closed ones. It lives across cycles because that
    #: leg answers 304 whenever nothing about an open pull request changed,
    #: and a 304 means unchanged rather than unknown. ``None`` until the
    #: first 200, which leaves the filter off -- see ``Classifier``.
    open_pull_requests: frozenset[int] | None = None

    def reload_config(self) -> None:
        """Re-read ``config.yaml`` and adopt its ``budget`` and ``publish``
        sections.

        ``budget.enabled: false`` is the emergency brake and
        ``publish.dry_run: true`` is the quieter one, so both must take
        effect without a restart. Only those two sections are swapped: a
        changed repository or store path mid-flight would mean the daemon's
        watermarks no longer describe what it is polling.

        A broken file leaves the previous configuration in force. Crashing
        here would turn the brake into a way to take the service down with a
        typo.
        """
        if self.config_path is None:
            return
        try:
            fresh = Config.load(self.config_path)
        except ConfigError as exc:
            logger.error("SIGHUP: keeping the previous configuration: %s", exc)
            return
        if (fresh.github, fresh.triggers, fresh.store) != (
            self.config.github,
            self.config.triggers,
            self.config.store,
        ):
            logger.warning(
                "SIGHUP: only the budget and publish sections are reloaded; "
                "changes to github, triggers or store need a restart"
            )
        self.config = replace(self.config, budget=fresh.budget, publish=fresh.publish)
        self.governor.reload(fresh.budget)
        self.publisher.reload(fresh.publish)
        logger.info(
            "SIGHUP: budget reloaded, enabled=%s session=%d weekly=%d; "
            "publish.dry_run=%s",
            fresh.budget.enabled,
            fresh.budget.session_limit,
            fresh.budget.weekly_limit,
            fresh.publish.dry_run,
        )

    def seed_watermarks(self, *, now: datetime) -> None:
        """Bound a fresh database to ``now`` before the first poll.

        Called once at startup, so the bound is process start rather than
        first-successful-poll -- which would drift later every time an early
        poll failed, widening the window of backlog treated as new.
        """
        for name in (PULL_REQUESTS, COMMENTS):
            self._since(name, now=now)

    async def run_once(self) -> CycleSummary:
        """Poll every endpoint once, and enqueue what the classifier accepts."""
        cycle = await self.poller.poll_once()
        return self._process(cycle, now=datetime.now(timezone.utc))

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Cycle until ``stop`` is set.

        A ``GitHubClientError`` is logged and the cycle skipped: a transient
        network failure must not kill a daemon. Anything else propagates --
        an unexpected bug should crash loudly rather than spin silently,
        because a daemon that keeps polling while failing to enqueue looks
        healthy and reviews nothing.
        """
        while not stop.is_set():
            try:
                await self.run_once()
            except GitHubClientError as exc:
                logger.error("poll cycle failed, retrying after the interval: %s", exc)
            await _wait(stop, self.poller.interval.seconds)

    def _process(self, cycle: PollCycle, *, now: datetime) -> CycleSummary:
        changed = cycle.changed_items()
        # The pulls leg runs first because it refreshes the set of open pull
        # requests the comments leg filters on, so a comment on a pull
        # request opened in this very cycle is still matched.
        pulls = self._pull_requests(changed.get(Endpoint.OPEN_PULLS), now=now)
        summary = pulls + self._comments(changed, now=now)
        logger.info("cycle seen=%d enqueued=%d", summary.seen, summary.enqueued)
        return summary

    def _pull_requests(
        self, items: list[dict] | None, *, now: datetime
    ) -> CycleSummary:
        """Classify a changed ``/pulls`` payload and enqueue what it accepts."""
        if items is None:
            return EMPTY
        since = self._since(PULL_REQUESTS, now=now)
        classifier = self.config.classifier(since)
        newest, summary, open_numbers = since, EMPTY, set()
        for pull in payloads.pull_requests(self.config.github.repo, items):
            newest = max(newest, pull.created_at)
            open_numbers.add(pull.number)
            summary += self._enqueue(classifier.classify_pull_request(pull), now=now)
        self.open_pull_requests = frozenset(open_numbers)
        self.store.advance_watermark(PULL_REQUESTS, newest)
        return summary

    def _comments(
        self, changed: dict[Endpoint, list[dict]], *, now: datetime
    ) -> CycleSummary:
        """Classify both changed comment payloads against one watermark.

        Both endpoints are repo-wide, so what they return is filtered to the
        open pull requests this sweep saw rather than by the endpoint.
        """
        batches = [
            changed[endpoint] for endpoint in COMMENT_ENDPOINTS if endpoint in changed
        ]
        if not batches:
            return EMPTY
        since = self._since(COMMENTS, now=now)
        classifier = self.config.classifier(since, self.open_pull_requests)
        newest, summary = since, EMPTY
        for batch in batches:
            for comment in payloads.comments(self.config.github.repo, batch):
                newest = max(newest, comment.updated_at)
                summary += self._enqueue(classifier.classify_comment(comment), now=now)
        self.store.advance_watermark(COMMENTS, newest)
        return summary

    def _enqueue(self, decision: Decision, *, now: datetime) -> CycleSummary:
        """One classified item: seen always, enqueued only when it is new."""
        if decision.trigger is None:
            return CycleSummary(seen=1, enqueued=0)
        added = self.queue.enqueue(decision.trigger, now=now)
        return CycleSummary(seen=1, enqueued=int(added))

    def _since(self, name: str, *, now: datetime) -> datetime:
        """The watermark in force for ``name``, seeding an unset one to ``now``."""
        stored = self.store.watermark(name)
        if stored is not None:
            return stored
        logger.info("cold start: seeding the %s watermark to %s", name, now.isoformat())
        return self.store.advance_watermark(name, now)


def build_engine(config: Config) -> ClaudeCliEngine:
    """The review engine ``config`` names.

    The one place a real, spending engine is constructed. ``FakeEngine``
    deliberately does not appear here: it is a test double, and a daemon
    running one would report reviews it never did.

    Only one adapter exists, so this does not dispatch on a name -- a
    registry keyed on a single entry would be a guess about the second
    adapter's shape, made before it exists. The ``engine`` section is
    required, so there is no fallback to choose either. The return type is
    the concrete adapter for the same reason: it is what this returns, and
    widening it to the protocol would claim a choice that is not being made.
    """
    engine = config.engine
    return ClaudeCliEngine(
        model=engine.model,
        expected_version=engine.expected_version,
        binary=engine.binary,
        timeout_seconds=engine.timeout_seconds,
        standards_paths=engine.standards_paths,
    )


def build_workers(
    daemon: Daemon,
    *,
    workspace: Workspace,
    engine: ReviewEngine,
    client: GitHubClient,
    endpoints: RepoEndpoints,
) -> list[ReviewWorker]:
    """The workers ``daemon``'s configuration asks for.

    They share the queue, the governor and the publisher, because all three
    are views of one SQLite file and one HTTP client: a second governor would
    measure the same windows and reach the same answers, but a second *queue*
    would be an invitation to forget that the lease is what serialises them,
    and a second publisher would be a second thing for ``SIGHUP`` to find.

    Each owner is distinct, and that is load-bearing rather than cosmetic:
    ``complete``, ``release``, ``abandon`` and ``settle`` are all guarded on
    it, so two workers sharing an owner could finish each other's rows.
    """
    return [
        ReviewWorker(
            queue=daemon.queue,
            governor=daemon.governor,
            workspace=workspace,
            engine=engine,
            client=client,
            endpoints=endpoints,
            publisher=daemon.publisher,
            runs=RunStore(daemon.store),
            owner=f"worker-{index + 1}-{uuid.uuid4().hex[:8]}",
        )
        for index in range(daemon.config.worker.count)
    ]


async def supervise(worker: ReviewWorker, stop: asyncio.Event) -> None:
    """Keep ``worker`` draining until ``stop``, across unexpected failures.

    An unexpected exception must not stop every review -- the poll loop would
    go on filling a queue nobody drains -- and must not spin silently either.
    So a crash is logged with its traceback and the loop re-entered after a
    delay that doubles up to a ceiling, returning to the floor whenever the
    worker got a review finished in between: progress means the fault was not
    persistent, so the accumulated delay is not earned.

    Because a worker holds no state between runs, re-entering the loop *is* a
    fresh worker; nothing is rebuilt.

    A poison pull request cannot drive this. An uncaught crash never releases
    the row, so it stays claimed under a live lease: the worker takes other
    work, and when the lease lapses the row is retried with its attempt
    counted, reaching ``abandoned`` after ``max_attempts``. The backoff is
    for the crash that is not about any row at all -- a full disk, a bug in
    the claim path -- where the worker dies holding nothing.
    """
    backoff = RESPAWN_BACKOFF
    while not stop.is_set():
        before = worker.completed
        try:
            await worker.run_forever(stop)
            return
        except Exception:  # pylint: disable=broad-except
            logger.exception("review worker %s crashed; restarting it", worker.owner)
            if worker.completed > before:
                backoff = RESPAWN_BACKOFF
            await _wait(stop, backoff)
            backoff = min(backoff * 2, RESPAWN_BACKOFF_MAX)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or until ``stop`` is set -- whichever comes first.

    A plain sleep would make a ``SIGTERM`` arriving early in a 600 s idle
    interval hang a service restart for the remainder of it.
    """
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _install_signal_handlers(stop: asyncio.Event, reload_config: Callable) -> None:
    """Set ``stop`` on ``SIGINT``/``SIGTERM``, reload on ``SIGHUP``.

    ``add_signal_handler`` is the asyncio-aware route, and the one that wakes
    the loop immediately. It is unimplemented on Windows, which CI
    spot-checks, so the plain handler is the fallback there -- and Windows
    has no ``SIGHUP`` at all, so that one is skipped rather than faked.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return
    try:
        loop.add_signal_handler(sighup, reload_config)
    except NotImplementedError:
        signal.signal(sighup, lambda *_: reload_config())


async def run(config: Config, token: str, config_path: Path | None = None) -> None:
    """Build the daemon ``config`` describes, and run it until stopped."""
    stop = asyncio.Event()
    path = Path(config.store.path).resolve()
    # A relative path is resolved against the working directory, and pointing
    # at the wrong file costs the queue's memory of what has been reviewed.
    logger.info("state database: %s", path)
    client = GitHubClient(token)
    endpoints = RepoEndpoints(config.github.owner, config.github.name)
    workspace = Workspace(config.github.repo, config.workspace.cache_dir)
    # Same reason as the store path above: the configured default is
    # relative, so what it means depends on where the daemon was started.
    logger.info("workspace cache: %s", workspace.cache_dir)
    try:
        with SqliteStore(path) as store:
            daemon = Daemon(
                config=config,
                poller=Poller(client=client, endpoints=endpoints, etags=store),
                store=store,
                queue=ReviewQueue(store),
                # The daemon owns the process, so it owns the governor its
                # workers claim through, and SIGHUP has something live to
                # reload.
                governor=Governor(store, config.budget),
                publisher=Publisher(
                    client=client,
                    endpoints=endpoints,
                    runs=RunStore(store),
                    config=config.publish,
                ),
                config_path=config_path,
            )
            _install_signal_handlers(stop, daemon.reload_config)
            daemon.seed_watermarks(now=datetime.now(timezone.utc))
            # Startup is the only safe moment to clear what a crashed run
            # left behind: no git of ours is running yet.
            await workspace.sweep()
            engine = build_engine(config)
            # Loud, because this is the line where the agent starts costing
            # money: every review from here is a real subprocess against a
            # metered plan, behind the governor and nothing else.
            logger.warning(
                "review engine %r (model %s) will spend real allowance; "
                "budget.enabled=%s, worker.count=%d",
                engine.name,
                config.engine.model,
                config.budget.enabled,
                config.worker.count,
            )
            workers = build_workers(
                daemon,
                workspace=workspace,
                engine=engine,
                client=client,
                endpoints=endpoints,
            )
            logger.info("draining the queue with %d worker(s)", len(workers))
            # The poll cycle and a review have different cadences -- 10-600 s
            # against minutes -- so they are separate tasks. A review must
            # never hold up a poll.
            await asyncio.gather(
                daemon.run_forever(stop),
                *[supervise(worker, stop) for worker in workers],
            )
    finally:
        await client.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the daemon from the command line; non-zero exit means unusable."""
    parser = argparse.ArgumentParser(description="pr-review-agent daemon")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config, token = startup(args.config)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    asyncio.run(run(config, token, Path(args.config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
