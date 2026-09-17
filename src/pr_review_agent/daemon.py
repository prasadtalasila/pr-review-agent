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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ._startup import StartupError, startup
from .budget import Governor
from .config import Config, ConfigError
from .poller import payloads
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import Endpoint, RepoEndpoints
from .poller.poller import PollCycle, Poller
from .queue import ReviewQueue
from .store import SqliteStore
from .triggers.models import Decision

logger = logging.getLogger(__name__)

TOKEN_ENV = "GITHUB_TOKEN"

#: The two watermark names STORAGE.md declares. Both comment endpoints feed
#: ``COMMENTS``: GitHub's ``updated`` only ever moves forward, so a single
#: high-water mark cannot hide a comment that surfaces later on the other.
PULL_REQUESTS = "pull_requests"
COMMENTS = "comments"

COMMENT_ENDPOINTS = (Endpoint.ISSUE_COMMENTS, Endpoint.REVIEW_COMMENTS)


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
    config_path: Path | None = None

    def reload_config(self) -> None:
        """Re-read ``config.yaml`` and adopt its ``budget`` section.

        ``budget.enabled: false`` is the emergency brake, so it must take
        effect without a restart. Only the budget section is swapped: a
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
                "SIGHUP: only the budget section is reloaded; changes to "
                "github, triggers or store need a restart"
            )
        self.config = replace(self.config, budget=fresh.budget)
        self.governor.reload(fresh.budget)
        logger.info(
            "SIGHUP: budget reloaded, enabled=%s session=%d weekly=%d",
            fresh.budget.enabled,
            fresh.budget.session_limit,
            fresh.budget.weekly_limit,
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
        summary = self._pull_requests(
            changed.get(Endpoint.OPEN_PULLS), now=now
        ) + self._comments(changed, now=now)
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
        newest, summary = since, EMPTY
        for pull in payloads.pull_requests(self.config.github.repo, items):
            newest = max(newest, pull.created_at)
            summary += self._enqueue(classifier.classify_pull_request(pull), now=now)
        self.store.advance_watermark(PULL_REQUESTS, newest)
        return summary

    def _comments(
        self, changed: dict[Endpoint, list[dict]], *, now: datetime
    ) -> CycleSummary:
        """Classify both changed comment payloads against one watermark."""
        batches = [
            changed[endpoint] for endpoint in COMMENT_ENDPOINTS if endpoint in changed
        ]
        if not batches:
            return EMPTY
        since = self._since(COMMENTS, now=now)
        classifier = self.config.classifier(since)
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
    try:
        with SqliteStore(path) as store:
            daemon = Daemon(
                config=config,
                poller=Poller(
                    client=client,
                    endpoints=RepoEndpoints(config.github.owner, config.github.name),
                    etags=store,
                ),
                store=store,
                queue=ReviewQueue(store),
                # Built here even though nothing claims yet: the daemon owns
                # the process, so it owns the governor a worker will claim
                # through, and SIGHUP has something live to reload.
                governor=Governor(store, config.budget),
                config_path=config_path,
            )
            _install_signal_handlers(stop, daemon.reload_config)
            daemon.seed_watermarks(now=datetime.now(timezone.utc))
            await daemon.run_forever(stop)
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
