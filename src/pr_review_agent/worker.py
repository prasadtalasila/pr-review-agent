"""The drainer: claim through the governor, review, settle, finish.

The daemon fills the queue; this empties it. Between those two lies the one
transition that costs money, so the whole module is arranged around three
rules.

**Nothing reaches an engine without the governor.** ``claim`` is called with
an ``admit`` predicate that consults the governor, so the reservation commits
in the same transaction as the lease and no review starts that was not paid
for in advance.

The predicate has exactly one bypass, and it is the reason the rule is stated
about *engines* rather than about claims. A pull request whose review is
already recorded and unposted is admitted without reserving anything, because
publishing it runs nothing and costs nothing. Without that bypass an
exhausted budget would hold a review the allowance has *already been spent
on* hostage until a window rolled -- refusing to spend nothing, to avoid a
cost that was paid days ago.

**Every run settles, including a failed one.** A reservation that is never
settled stays charged at its full ceiling until it ages out of a rolling
window. That is the deliberate behaviour for a *lost* worker -- one whose
process died -- but a caught exception is not a lost worker. What a caught
failure settles at depends on the one thing that is knowable: whether the
engine had started. Before it, nothing reached an engine and the run settles
at zero; at or after it, a killed CLI adapter may have spent anything, so the
run settles at its full reservation. Pessimism is the safe direction for a
spending control.

**A paid review is published, or kept until it can be.** The engine is the
only irreversible step, so the order after it is fixed: settle, record,
publish, finish. Recording before publishing is what lets a GitHub write
fail without costing a second review -- the next claim finds the unpublished
run and posts it without reaching an engine at all. That resume path is the
one way into this class that spends nothing: it reserves nothing, settles
nothing, writes no ledger row, and hands the row back unattempted if the post
fails again.

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

from .budget import Governor, StopReason, Usage, UsageConfidence
from .engine import EngineTimeout, Outcome, ReviewEngine, ReviewRequest, ReviewResult
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import RepoEndpoints
from .poller.pulls import fetch_pull_request_facts
from .publisher import Publisher, PublishOutcome
from .queue import Claim, ReviewQueue
from .runs import RecordedRun, RunStore
from .triggers.models import PayloadError
from .workspace import PullRequestTooLarge, Workspace, WorkspaceError

logger = logging.getLogger(__name__)

#: How long to wait when there is nothing claimable. A claim is a local
#: SQLite read, so polling it is nearly free -- the number is chosen for the
#: log rather than the load. While the governor is refusing, every attempt
#: logs a warning and the row stays pending, so a five-second loop would
#: write some 720 identical lines an hour into an operator's journal.
WORKER_IDLE = 30.0

#: How a finished run's outcome reads on the ledger. Kept apart from
#: ``_finish_for``, which decides the queue row's fate: what happened and what
#: to do about it are different questions, and one mapping answering both
#: would tie them together for no reason.
_REASON_FOR = {
    Outcome.COMPLETED: StopReason.COMPLETED,
    Outcome.TRUNCATED: StopReason.TRUNCATED,
    Outcome.FAILED: StopReason.FAILED,
}


class EngineError(RuntimeError):
    """A review engine failed, whatever it failed at.

    Every adapter is a foreign command-line tool, so the worker has no
    catalogue of what one can raise and no way to tell a timeout from a
    parse error from a bug inside it. All of them are the same fact here --
    this run produced no review -- and all of them are worth another attempt.
    Narrowing the boundary to this one call is what keeps a bug in the
    *worker* propagating to the supervisor instead of being retried three
    times in silence.

    One distinction survives the flattening, and only for the ledger: a run
    killed by its own wall clock is the agent's per-run ceiling doing its
    job, and a run whose tool fell over is not. The retry decision does not
    branch on it -- both are retried -- but an operator counting rows needs
    to know which of the two they are looking at.
    """

    def __init__(self, message: str, reason: StopReason) -> None:
        super().__init__(message)
        self.reason = reason


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
    publisher: Publisher
    runs: RunStore
    owner: str
    #: Runs that reached the end of a review. Read by the supervisor.
    completed: int = 0

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Drain the queue until ``stop`` is set."""
        while not stop.is_set():
            if not await self.run_once():
                await _wait(stop, WORKER_IDLE)

    def admit(self, conn, claim: Claim, now: datetime) -> bool:
        """Whether this claim may be taken: the governor, plus one bypass.

        Runs inside the claim transaction, which is why the connection is
        passed down rather than a new one opened.

        A pull request carrying a recorded, unpublished run is admitted
        unconditionally and **reserves nothing**. That run reached an engine
        once, under a reservation that has already settled; posting it
        reaches none. Weighing it against a window would refuse to spend
        nothing.
        """
        if self.runs.has_unpublished(conn, claim.trigger.repo, claim.trigger.pr_number):
            return True
        return self.governor.admit(conn, claim, now)

    async def run_once(self) -> bool:
        """Claim and review one trigger; ``False`` if nothing was claimable.

        Nothing claimable and a refusal by the governor are the same answer
        here, and deliberately so: both mean there is no work this worker may
        do, and neither is a failure.
        """
        claim = self.queue.claim(now=_now(), owner=self.owner, admit=self.admit)
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
        # Before the reservation lookup, not after: a resume is admitted
        # without reserving, so it has no ledger row for `admitted_mode` to
        # find and would read as a lapsed lease.
        if await self._resume_publication(claim):
            return

        mode = self.governor.admitted_mode(claim)
        if mode is None:
            # The lease lapsed and somebody else holds this row. Touching
            # either the ledger or the queue now would corrupt their run.
            logger.warning("lease lapsed before %s started", claim.trigger.dedupe_key)
            return
        # After the claim, before anything slow. The acknowledgement has to
        # beat a review that takes minutes, and it is what makes an adaptive
        # poll interval feel like an answer rather than a silence.
        await self.publisher.acknowledge(claim.trigger)

        usage = Usage(0, UsageConfidence.UNAVAILABLE, engine=self.engine.name)
        # Everything that can fail before the engine starts is infrastructure,
        # so it stands as the answer until something narrows it.
        reason = StopReason.INFRASTRUCTURE
        finish = self.queue.complete
        reviewed: RecordedRun | None = None
        try:
            facts = await fetch_pull_request_facts(
                self.client, self.endpoints, claim.trigger.pr_number
            )
            config = self.governor.config
            async with self.workspace.checkout(
                facts,
                max_changed_files=config.max_changed_files,
                max_changed_lines=config.max_changed_lines,
                excluded_paths=config.excluded_paths,
            ) as checkout:
                if not self.governor.preflight(claim, checkout.reviewed.lines, _now()):
                    # The last free refusal, and it released the reservation
                    # inside that call -- so this path must not settle again.
                    # Deterministic for this head, so the row ends here.
                    self.queue.abandon(claim)
                    return
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
            usage, finish = result.usage, self._finish_for(result.outcome)
            reason = _REASON_FOR[result.outcome]
            if result.outcome is Outcome.COMPLETED:
                self.completed += 1
                # Recorded before the settle so the content outlives any
                # failure after it. Only a completed run has publishable
                # findings -- the seam enforces that -- so only one is kept.
                reviewed = self.runs.record(
                    claim.trigger,
                    head_sha=facts.head_sha,
                    result=result,
                    now=_now(),
                )
            logger.info(
                "reviewed %s: %s, %d findings, %d tokens",
                claim.trigger.dedupe_key,
                result.outcome,
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
        except EngineError as exc:
            logger.warning(
                "%s failed and will be retried", claim.trigger.dedupe_key, exc_info=True
            )
            # The only handler that narrows the reason: the adapter already
            # told us whether its own wall clock stopped it.
            reason = exc.reason
            finish = self.queue.release
        except (GitHubClientError, WorkspaceError):
            logger.warning(
                "%s failed and will be retried", claim.trigger.dedupe_key, exc_info=True
            )
            finish = self.queue.release

        await self._settle_publish_and_finish(claim, usage, reason, finish, reviewed)

    def _finish_for(self, outcome: Outcome) -> Callable[[Claim], bool]:
        """Which queue verb closes a row whose run ended this way.

        ``TRUNCATED`` is a run cut off with work outstanding, so another
        attempt is worth its allowance; ``FAILED`` is anything else that went
        wrong, which is not. The engine has already been paid for either way
        -- the outcome decides the row's fate, never whether it settles.
        """
        if outcome is Outcome.COMPLETED:
            return self.queue.complete
        if outcome is Outcome.TRUNCATED:
            return self.queue.release
        return self.queue.abandon

    async def _review(self, request: ReviewRequest) -> ReviewResult:
        """Run the engine, converting any failure of it into ``EngineError``."""
        try:
            return await self.engine.review(request)
        except EngineTimeout as exc:
            raise EngineError(
                f"{self.engine.name} outlived its wall clock: {exc}",
                StopReason.TIMEOUT,
            ) from exc
        except Exception as exc:  # the adapter is a foreign tool; see EngineError
            raise EngineError(
                f"{self.engine.name} failed: {exc}", StopReason.ENGINE_ERROR
            ) from exc

    async def _resume_publication(self, claim: Claim) -> bool:
        """Publish a run an earlier attempt paid for but could not post.

        ``True`` when this claim was spent on that and nothing else. No
        facts are fetched, no checkout is made, no engine is reached and --
        because :meth:`admit` let this claim through without reserving --
        there is nothing on the ledger to settle. A resume costs nothing and
        writes nothing, which is the honest record of it.

        Taking the ordinary claim rather than a path of its own is what
        keeps one pull request in one worker's hands: a second lease would
        be a second chance to post the same comment twice. The lease is
        re-checked immediately before the write, because the owner guards on
        ``complete`` and ``release`` only run *after* it -- late enough to
        discard a row, too late to unsay a comment.

        A failure hands the row back **unattempted**. The attempt bound caps
        what one poison trigger may drain, and a post that reached no engine
        drained nothing; counting it would abandon a review after three
        failed posts and leave its findings recorded and permanently
        invisible.
        """
        pending = self.runs.unpublished_for(claim.trigger.repo, claim.trigger.pr_number)
        if pending is None:
            return False
        logger.info(
            "%s was reviewed already; publishing without a second review",
            pending.dedupe_key,
        )
        if not self.queue.holds(claim, now=_now()):
            logger.warning("lease lapsed before %s republished", pending.dedupe_key)
            return True
        if await self._published(claim, pending):
            self.queue.complete(claim)
        else:
            self.queue.release_unattempted(claim)
        return True

    async def _settle_publish_and_finish(
        self,
        claim: Claim,
        usage: Usage,
        reason: StopReason,
        finish: Callable[[Claim], bool],
        reviewed: RecordedRun | None,
    ) -> None:
        """Record what the run cost, post it, then close its row.

        All three are guarded on the owner. A worker whose lease lapsed
        mid-run gets ``False`` from ``settle`` and stops there, which is how
        it learns to discard a result it is no longer entitled to publish --
        now with teeth, because the thing being discarded is a comment under
        the agent's own account.
        """
        if not self.governor.settle(claim, usage, now=_now(), stop_reason=reason):
            logger.warning(
                "no reservation to settle for %s: discarding the run",
                claim.trigger.dedupe_key,
            )
            return
        if reviewed is not None:
            # This attempt *did* reach an engine, so a failed post counts
            # against the bound like any other retry -- unlike the
            # publish-only resume, which spent nothing.
            finish = (
                self.queue.complete
                if await self._published(claim, reviewed)
                else self.queue.release
            )
        finish(claim)

    async def _published(self, claim: Claim, run: RecordedRun) -> bool:
        """Post ``run``; ``False`` if the row should be handed back.

        A failed write is worth another attempt: the findings are already
        durable, so the retry republishes rather than re-reviewing.

        A superseded head is not. Nothing here will ever make it match again
        -- a push to an existing pull request is not a trigger -- so another
        attempt would re-read the same stale sha, and if it re-reviewed it
        would reserve allowance to do it.

        A boolean rather than the queue verb itself: the two callers hand the
        row back differently, one counting the attempt and one not, and a
        method that returned ``self.queue.release`` would invite comparing
        bound methods for identity, which does not work.
        """
        key = claim.trigger.dedupe_key
        try:
            published = await self.publisher.publish(run)
        except (GitHubClientError, PayloadError):
            logger.warning(
                "could not publish %s; the review is kept and will be "
                "posted without being run again",
                key,
                exc_info=True,
            )
            return False
        if published.outcome is PublishOutcome.SUPERSEDED:
            logger.info("%s was superseded before it could be posted", key)
        return True


def _now() -> datetime:
    """The current instant, aware and in UTC, as the store requires."""
    return datetime.now(timezone.utc)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or until ``stop`` is set -- whichever comes first."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
