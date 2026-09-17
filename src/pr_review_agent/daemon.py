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

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .poller import payloads
from .poller.endpoints import Endpoint
from .poller.poller import PollCycle, Poller
from .queue import ReviewQueue
from .store import SqliteStore
from .triggers.models import Decision

logger = logging.getLogger(__name__)

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
