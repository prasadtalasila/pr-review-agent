"""The drainer: claim through the governor, review, settle, finish.

The daemon fills the queue; this empties it. Between those two lies the one
transition that costs money, so the whole module is arranged around three
rules.

**Nothing claims without the governor.** ``claim`` is called with
``admit=governor.admit`` and never without it, so the reservation commits in
the same transaction as the lease. The worker cannot start a run it did not
pay for in advance.

**Every run settles, including a failed one.** A reservation that is never
settled stays charged at its full ceiling until it ages out of a rolling
window. That is the deliberate behaviour for a *lost* worker -- one whose
process died -- but a caught exception is not a lost worker. What a caught
failure settles at depends on the one thing that is knowable: whether the
engine had started. Before it, nothing reached an engine and the run settles
at zero; at or after it, a killed CLI adapter may have spent anything, so the
run settles at its full reservation. Pessimism is the safe direction for a
spending control.

**A run leaves nothing behind.** No field on this class outlives the run that
set it, because the tree under review is untrusted input and the next review
must not be able to see it. The checkout is removed by its context manager,
the engine is a subprocess with its own working directory, and what remains
shared -- the bare mirror -- is bounded by the workspace's own rules. The one
counter that does persist, ``completed``, holds no run data: the supervisor
reads it to tell a worker that is making progress from one that is crash
looping.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .budget import Governor, Usage, UsageConfidence
from .engine import ReviewEngine, ReviewRequest, ReviewResult
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import RepoEndpoints
from .poller.pulls import fetch_pull_request_facts
from .queue import Claim, ReviewQueue
from .triggers.models import PayloadError
from .workspace import PullRequestTooLarge, Workspace, WorkspaceError

logger = logging.getLogger(__name__)

#: How long to wait when there is nothing claimable. A claim is a local
#: SQLite read, so polling it is nearly free -- the number is chosen for the
#: log rather than the load. While the governor is refusing, every attempt
#: logs a warning and the row stays pending, so a five-second loop would
#: write some 720 identical lines an hour into an operator's journal.
WORKER_IDLE = 30.0


class EngineError(RuntimeError):
    """A review engine failed, whatever it failed at.

    Every adapter is a foreign command-line tool, so the worker has no
    catalogue of what one can raise and no way to tell a timeout from a
    parse error from a bug inside it. All of them are the same fact here --
    this run produced no review -- and all of them are worth another attempt.
    Narrowing the boundary to this one call is what keeps a bug in the
    *worker* propagating to the supervisor instead of being retried three
    times in silence.
    """


@dataclass
class ReviewWorker:
    """One review at a time: claim, run, settle, finish."""

    # Every field is a collaborator this class joins, and joining them is the
    # whole job: the queue says what to review, the governor whether it may,
    # the workspace where, the engine how. Bundling them into a "context"
    # object would hide the dependency list without shortening it.
    # pylint: disable=too-many-instance-attributes

    queue: ReviewQueue
    governor: Governor
    workspace: Workspace
    engine: ReviewEngine
    client: GitHubClient
    endpoints: RepoEndpoints
    owner: str
    #: Runs that reached the end of a review. Read by the supervisor.
    completed: int = 0

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Drain the queue until ``stop`` is set."""
        while not stop.is_set():
            if not await self.run_once():
                await _wait(stop, WORKER_IDLE)

    async def run_once(self) -> bool:
        """Claim and review one trigger; ``False`` if nothing was claimable.

        Nothing claimable and a refusal by the governor are the same answer
        here, and deliberately so: both mean there is no work this worker may
        do, and neither is a failure.
        """
        claim = self.queue.claim(
            now=_now(), owner=self.owner, admit=self.governor.admit
        )
        if claim is None:
            return False
        await self.run_one(claim)
        return True

    async def run_one(self, claim: Claim) -> None:
        """Review one claim, settle it, and finish its row.

        The exception handlers are the retry taxonomy. A transient failure --
        a 5xx, a rate limit, a failed fetch, an engine that died -- hands the
        row back for another attempt, bounded by ``max_attempts``. A
        deterministic one abandons it: an oversized pull request and an
        unusable payload will fail identically on attempt two, having
        reserved allowance again to do it.
        """
        mode = self.governor.admitted_mode(claim)
        if mode is None:
            # The lease lapsed and somebody else holds this row. Touching
            # either the ledger or the queue now would corrupt their run.
            logger.warning("lease lapsed before %s started", claim.trigger.dedupe_key)
            return

        usage = Usage(0, UsageConfidence.UNAVAILABLE, engine=self.engine.name)
        finish = self.queue.complete
        try:
            facts = await fetch_pull_request_facts(
                self.client, self.endpoints, claim.trigger.pr_number
            )
            config = self.governor.config
            async with self.workspace.checkout(
                facts,
                max_changed_files=config.max_changed_files,
                max_changed_lines=config.max_changed_lines,
            ) as checkout:
                # From here on a failure may have cost tokens, so it settles
                # at the ceiling it reserved. The assignment sits on the line
                # before the call for exactly that reason.
                usage = replace(usage, tokens=config.max_run_tokens)
                result = await self._review(
                    ReviewRequest(
                        checkout=checkout,
                        facts=facts,
                        trigger=claim.trigger,
                        mode=mode,
                    )
                )
            usage = result.usage
            self.completed += 1
            logger.info(
                "reviewed %s: %d findings, %d tokens",
                claim.trigger.dedupe_key,
                len(result.findings),
                usage.tokens,
            )
        # PullRequestTooLarge subclasses WorkspaceError, so it is caught
        # first or it would be retried.
        except (PullRequestTooLarge, PayloadError):
            logger.warning(
                "giving up on %s permanently", claim.trigger.dedupe_key, exc_info=True
            )
            finish = self.queue.abandon
        except (GitHubClientError, WorkspaceError, EngineError):
            logger.warning(
                "%s failed and will be retried", claim.trigger.dedupe_key, exc_info=True
            )
            finish = self.queue.release

        self._settle_and_finish(claim, usage, finish)

    async def _review(self, request: ReviewRequest) -> ReviewResult:
        """Run the engine, converting any failure of it into ``EngineError``."""
        try:
            return await self.engine.review(request)
        except Exception as exc:  # the adapter is a foreign tool; see EngineError
            raise EngineError(f"{self.engine.name} failed: {exc}") from exc

    def _settle_and_finish(
        self, claim: Claim, usage: Usage, finish: Callable[[Claim], bool]
    ) -> None:
        """Record what the run cost, then close its row -- in that order.

        Both are guarded on the owner. A worker whose lease lapsed mid-run
        gets ``False`` from ``settle`` and stops there, which is how it
        learns to discard a result it is no longer entitled to publish.
        """
        if not self.governor.settle(claim, usage, now=_now()):
            logger.warning(
                "no reservation to settle for %s: discarding the run",
                claim.trigger.dedupe_key,
            )
            return
        finish(claim)


def _now() -> datetime:
    """The current instant, aware and in UTC, as the store requires."""
    return datetime.now(timezone.utc)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or until ``stop`` is set -- whichever comes first."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
