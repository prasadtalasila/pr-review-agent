"""The drainer: claim through the governor, review, settle, finish.

The daemon fills the queue; this empties it. Between those two lies the one
transition that costs money, so the whole module is arranged around three
rules.

**Nothing reaches an engine without the governor.** ``claim`` is called with
an ``admit`` predicate that consults the governor, so the reservation commits
in the same transaction as the lease and no review starts that was not paid
for in advance.

The predicate has exactly one exemption, and it is the reason the rule is
stated about *engines* rather than about claims: a ``PUBLISH`` item is
admitted without reserving anything, because posting a review that has
already been paid for runs nothing and costs nothing. Without it an
exhausted budget would hold a review the allowance has *already been spent
on* hostage until a window rolled -- refusing to spend nothing, to avoid a
cost that was paid days ago. The exemption is read off the item's kind
alone: admission asks what this row is, never what some other row left
behind.

**Every run settles, including a failed one.** A reservation that is never
settled stays charged at its full ceiling until it ages out of a rolling
window. That is the deliberate behaviour for a *lost* worker -- one whose
process died -- but a caught exception is not a lost worker. What a caught
failure settles at depends on the one thing that is knowable: whether the
engine had started. Before it, nothing reached an engine and the run settles
at zero; at or after it, a killed CLI adapter may have spent anything, so the
run settles at its full reservation. Pessimism is the safe direction for a
spending control. The one failure *at* the engine that provably ran nothing
-- ``EngineUnavailable``, a subprocess that never started -- settles at zero
too, because there was no process to spend.

**A paid review is published, or kept until it can be.** The engine is the
only irreversible step, so the order after it is fixed: settle, record,
publish, finish. Recording before publishing is what lets a GitHub write
fail without costing a second review -- the worker enqueues a ``PUBLISH``
item naming that run, and posting it reaches no engine at all. Republication
is therefore its own unit of work rather than something the next trigger for
that pull request is spent on: a mention always reviews the head it was
written against. The item is the one way into this class that spends
nothing: it reserves nothing, settles nothing, writes no ledger row, and
hands its row back unattempted if the post fails again.

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
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from ._compat import StrEnum
from ._time import now as _now
from ._time import wait_until
from .budget import Governor, Mode, StopReason, Usage, UsageConfidence
from .engine import (
    EngineProtocolError,
    EngineTimeout,
    EngineUnavailable,
    Outcome,
    ReviewEngine,
    ReviewRequest,
    ReviewResult,
    UsageLimited,
)
from .numbering import assign
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import RepoEndpoints
from .poller.pulls import fetch_pull_request_facts
from .publisher import Publisher, PublishOutcome
from .queue import Claim, ReviewQueue
from .runs import (
    PullRequestHistory,
    RecordedRun,
    RunStore,
    publication_of,
    published_run_key,
)
from .triggers.models import PayloadError, TriggerKind
from .workspace import (
    PullRequestFacts,
    PullRequestTooLarge,
    Workspace,
    WorkspaceError,
)

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


class Finish(StrEnum):
    """Which queue verb closes a row, named rather than bound.

    A name is what lets :func:`classify_failure` stay a pure function over
    an exception: the verbs themselves are methods on one worker's queue,
    and a table of bound methods could not be read, compared or tested
    without one.
    """

    COMPLETE = "complete"
    RELEASE = "release"
    RELEASE_UNATTEMPTED = "release_unattempted"
    ABANDON = "abandon"


@dataclass(frozen=True)
class RunEnd:
    """How one run ended: what it cost, why it stopped, what closes its row.

    ``reviewed`` and ``reviewed_lines`` are set only by a run that finished
    a review -- the recorded findings to post, and the line count the
    pre-flight estimate is fitted against.
    """

    usage: Usage
    reason: StopReason
    finish: Finish
    reviewed: RecordedRun | None = None
    reviewed_lines: int | None = None


@dataclass
class _Spend:
    """What a failure would settle at, as the run moves past the engine.

    Mutable and passed down on purpose: the answer changes at exactly one
    line -- the one before the engine call -- and the caller has to know
    which side of it a raised exception came from. Before it nothing reached
    an engine and the zero is provable; at or after it a killed adapter may
    have spent anything, and pessimism is the safe direction for a spending
    control.
    """

    usage: Usage


def _log_start(claim: Claim, mode: Mode) -> None:
    """The start of a review, here rather than in the engine adapter.

    The adapter's line fires only once the pull request facts and the
    checkout have both succeeded, and a cold clone is the slow part -- so a
    run that stalls there would be indistinguishable from one that never
    started. Held to the moment the lease is confirmed and nothing slow has
    been attempted, this is the record that makes *started and still going*
    a different thing from *never started*.
    """
    logger.info(
        "reviewing %s#%d as %s (mode=%s)",
        claim.trigger.repo,
        claim.trigger.pr_number,
        claim.trigger.dedupe_key,
        mode,
        extra={
            "repo": claim.trigger.repo,
            "pr": claim.trigger.pr_number,
            "mode": mode,
        },
    )


def _log_closed(claim: Claim, facts: PullRequestFacts) -> None:
    """Why nothing was reviewed, and what an operator can do about it.

    The two cases read differently and the difference is not cosmetic. A
    merged pull request is over, and there is nothing to say beyond what
    was skipped. A ``closed`` one can be reopened -- and reopening it
    brings nothing back, because every trigger in this design is gated on a
    timestamp that has already passed (``pr_opened`` on ``created_at``, a
    mention on the comment's ``updated_at``) and this row's dedupe key
    blocks it being enqueued a second time. A fresh ``@claude`` comment
    after the reopen does work, and it is the only thing that does, so the
    line says so rather than leaving an operator to discover it.
    """
    recovery = (
        ""
        if facts.merged
        else " Reopening it re-triggers nothing; a fresh @claude comment does."
    )
    logger.info(
        "%s was %s before it was reviewed, so nothing was spent on it.%s",
        claim.trigger.dedupe_key,
        "merged" if facts.merged else facts.state,
        recovery,
        extra={
            "repo": claim.trigger.repo,
            "pr": claim.trigger.pr_number,
            "state": facts.state,
            "merged": facts.merged,
        },
    )


def _log_reviewed(claim: Claim, result: ReviewResult, usage: Usage) -> None:
    """What the review produced and what it cost."""
    logger.info(
        "reviewed %s: %s, %d findings, %d tokens",
        claim.trigger.dedupe_key,
        result.outcome,
        len(result.findings),
        usage.tokens,
        extra={
            "repo": claim.trigger.repo,
            "pr": claim.trigger.pr_number,
            "outcome": result.outcome,
            "findings": len(result.findings),
            "tokens": usage.tokens,
        },
    )


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

    ``status`` is the API status the adapter read, when it read one. It is
    the only part of the adapter's failure that reaches the pull request:
    see :func:`failure_notice`.
    """

    def __init__(
        self, message: str, reason: StopReason, *, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


#: Every failure the taxonomy has an answer for. Anything else is a bug in
#: the worker rather than a review that went wrong, and propagates to the
#: supervisor instead of being retried three times in silence.
_FAILURES = (
    PullRequestTooLarge,
    PayloadError,
    UsageLimited,
    EngineUnavailable,
    EngineError,
    GitHubClientError,
    WorkspaceError,
)


def classify_failure(exc: Exception, reserved: Usage) -> RunEnd:
    """What a caught failure cost, why it stopped, and how its row closes.

    Pure, so each arm is a table row a test can state directly. Ordered by
    specificity rather than by likelihood: ``PullRequestTooLarge`` subclasses
    ``WorkspaceError``, so it is matched first or it would be retried.

    ``reserved`` is what the run would settle at if the failure says nothing
    more precise -- zero before the engine was reached, the reserved ceiling
    after it.
    """
    if isinstance(exc, PullRequestTooLarge | PayloadError):
        # Deterministic: attempt two fails identically, having reserved
        # allowance again to do it.
        return RunEnd(reserved, StopReason.INFRASTRUCTURE, Finish.ABANDON)
    if isinstance(exc, UsageLimited):
        # Knowable, in both of its shapes: measured if the engine printed an
        # envelope, zero if it was refused before doing any work. And
        # unattempted -- the bound caps what one poison trigger may drain,
        # and this one drained nothing; the account was already out when it
        # arrived.
        return RunEnd(
            exc.usage or Usage(0, UsageConfidence.EXACT),
            StopReason.USAGE_LIMIT,
            Finish.RELEASE_UNATTEMPTED,
        )
    if isinstance(exc, EngineUnavailable):
        # No process was created, so the zero is provable and the ceiling
        # would write tokens that were never spent into all three rolling
        # windows. Still counted, unlike a usage limit: this clears when an
        # operator acts, not when a window rolls, so the attempt bound is
        # what stops a misconfigured host retrying every trigger forever.
        return RunEnd(
            replace(reserved, tokens=0), StopReason.ENGINE_UNAVAILABLE, Finish.RELEASE
        )
    if isinstance(exc, EngineError):
        # The only arm that narrows the reason: the adapter already told us
        # whether its own wall clock stopped it.
        return RunEnd(reserved, exc.reason, Finish.RELEASE)
    return RunEnd(reserved, StopReason.INFRASTRUCTURE, Finish.RELEASE)


def failure_notice(exc: Exception) -> str | None:
    """What a pull request is told when its last attempt failed this way.

    ``None`` for everything that is not the engine's: a GitHub or git
    failure says nothing a contributor can act on, and the post announcing
    it would likely fail the same way.

    Fixed text over an integer, never the failure's message. The message is
    whatever the tool printed -- host paths, account and quota state -- and
    this goes under the agent's account on what may be a public repository.
    The operator's journal has the whole of it.
    """
    if isinstance(exc, EngineUnavailable):
        return "The review engine could not be started on the agent's host."
    if not isinstance(exc, EngineError):
        return None
    if exc.reason is StopReason.TIMEOUT:
        return "The review engine ran out of time."
    api = "" if exc.status is None else f" (API error {exc.status})"
    return f"The review engine failed{api}."


def _finish_for(outcome: Outcome) -> Finish:
    """Which queue verb closes a row whose run ended this way.

    ``TRUNCATED`` is a run cut off with work outstanding, so another attempt
    is worth its allowance; ``FAILED`` is anything else that went wrong,
    which is not. The engine has already been paid for either way -- the
    outcome decides the row's fate, never whether it settles.
    """
    if outcome is Outcome.COMPLETED:
        return Finish.COMPLETE
    if outcome is Outcome.TRUNCATED:
        return Finish.RELEASE
    return Finish.ABANDON


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
                await wait_until(stop, WORKER_IDLE)

    def admit(self, conn, claim: Claim, now: datetime) -> bool:
        """Whether this claim may be taken: the governor, and one exemption.

        Runs inside the claim transaction, which is why the connection is
        passed down rather than a new one opened.

        A ``PUBLISH`` item is admitted unconditionally and **reserves
        nothing**. The run it names reached an engine once, under a
        reservation that has already settled; posting it reaches none, so
        weighing it against a window would refuse to spend nothing.

        The question is asked of the item itself. It used to be asked of the
        pull request -- *is any run here unposted?* -- which let one
        trigger's claim be spent finishing another trigger's run, and made a
        fresh mention mean whatever the oldest pending run happened to be.
        """
        if claim.trigger.kind is TriggerKind.PUBLISH:
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

        The failure taxonomy is :func:`classify_failure`: a transient failure
        -- a 5xx, a rate limit, a failed fetch, an engine that died -- hands
        the row back for another attempt, bounded by ``max_attempts``. A
        deterministic one abandons it: an oversized pull request and an
        unusable payload will fail identically on attempt two, having
        reserved allowance again to do it.

        Two of those endings say so on the pull request rather than only in
        the journal -- the size gate here and the pre-flight in
        :meth:`_attempt`. Both are refusals a reader can act on, and both
        would otherwise leave a 👀 as the last thing the agent ever said
        (issue #78). See :meth:`Publisher.notify`.

        So does an engine failure, but only on the last attempt it is
        allowed: an earlier one may yet be fixed by the retry, and then there
        is nothing to announce. See :func:`failure_notice`.
        """
        # Before the reservation lookup, not after: a publication item is
        # admitted without reserving, so it has no ledger row for
        # `admitted_mode` to find and would read as a lapsed lease.
        if claim.trigger.kind is TriggerKind.PUBLISH:
            await self._publish_recorded(claim)
            return

        # The fold boundary: every trigger for this pull request already
        # waiting at this moment is answered by the review about to run.
        started = _now()
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
        _log_start(claim, mode)

        # Everything that can fail before the engine starts is provably
        # free, so the run would settle at nothing until `_attempt` reaches
        # the engine and raises the floor to the ceiling it reserved.
        spend = _Spend(Usage(0, UsageConfidence.UNAVAILABLE, engine=self.engine.name))
        failure: Exception | None = None
        try:
            end = await self._attempt(claim, mode, spend)
        except _FAILURES as exc:
            end = self._failed(claim, exc, spend.usage)
            failure = exc
        if end is None:
            return
        await self._settle_publish_and_finish(claim, end)
        if failure is not None:
            await self._announce(claim, failure, end)
        if end.reviewed is not None:
            self._fold(claim, started, end.reviewed.head_sha)

    async def _announce(self, claim: Claim, exc: Exception, end: RunEnd) -> None:
        """Say on the pull request why this run ended, if a reader should know.

        After the row is closed, never before: the notice is a courtesy and
        the bookkeeping is the record.
        """
        if isinstance(exc, PullRequestTooLarge):
            # The one refusal a contributor can act on. Every other arm of
            # the taxonomy is retried or an agent-side bug, and of those only
            # an engine failure with no attempts left is announced, below.
            await self.publisher.notify(claim.trigger, exc.notice)
            return
        if end.finish is not Finish.RELEASE or claim.attempts < self.queue.max_attempts:
            return
        notice = failure_notice(exc)
        if notice is not None:
            await self.publisher.report_failure(
                claim.trigger, notice, attempts=claim.attempts
            )

    def _fold(self, claim: Claim, started: datetime, head_sha: str) -> None:
        """Close the triggers this review answered, and say how many.

        Only a run that recorded findings folds anything. Its comment is
        either posted or owed by a publication item naming durable findings,
        so the waiting triggers have their answer either way -- whereas a run
        that recorded nothing has nothing to answer them with.
        """
        folded = self.queue.fold(claim, before=started, head_sha=head_sha)
        if folded:
            logger.info(
                "folded %d waiting trigger(s) on %s#%d into %s",
                folded,
                claim.trigger.repo,
                claim.trigger.pr_number,
                claim.trigger.dedupe_key,
                extra={"repo": claim.trigger.repo, "pr": claim.trigger.pr_number},
            )

    async def _attempt(self, claim: Claim, mode: Mode, spend: _Spend) -> RunEnd | None:
        """Run one review; ``None`` when the pre-flight ended the row itself."""
        facts = await fetch_pull_request_facts(
            self.client, self.endpoints, claim.trigger.pr_number
        )
        if not facts.is_open:
            self._abandon_closed(claim, facts)
            return None
        config = self.governor.config
        # Read before the engine runs, so the reviewer can be shown what the
        # last round found. Costs one query on a table the worker already
        # writes; spends nothing and reaches no engine.
        history = self.runs.history(claim.trigger.repo, claim.trigger.pr_number)
        async with self.workspace.checkout(
            facts,
            max_changed_files=config.max_changed_files,
            max_changed_lines=config.max_changed_lines,
            excluded_paths=config.excluded_paths,
        ) as checkout:
            # Captured here because the checkout is torn down by the time the
            # run settles, and it is the worker's own number rather than the
            # adapter's: see `Governor.settle`.
            lines = checkout.reviewed.lines
            refused = self.governor.preflight(claim, lines, _now())
            if refused is not None:
                # The last free refusal, and it released the reservation
                # inside that call -- so this path must not settle again.
                # Deterministic for this head, so the row ends here.
                self.queue.abandon(claim)
                # After the row is closed, never before: the notice is a
                # courtesy and the bookkeeping is the record.
                await self.publisher.notify(claim.trigger, refused)
                return None
            # From here on a failure may have cost tokens, so it settles at
            # the ceiling it reserved. The assignment sits on the line before
            # the call for exactly that reason.
            spend.usage = replace(spend.usage, tokens=config.max_run_tokens)
            result = await self._review(
                ReviewRequest(
                    checkout=checkout,
                    facts=facts,
                    trigger=claim.trigger,
                    mode=mode,
                    prior=history.prior,
                )
            )
        return self._reviewed(claim, result, facts=facts, history=history, lines=lines)

    def _abandon_closed(self, claim: Claim, facts: PullRequestFacts) -> None:
        """End a claim whose pull request was merged or closed while it waited.

        Placed before the checkout because that is where the money starts:
        a trigger is queued while the pull request is open and claimed
        some time later, and nothing between those two moments asks GitHub
        whether it still is. Without this the agent clones the tree, runs
        the engine and posts a review on a pull request nobody will read --
        paid for in full. The read that answers the question is the one
        ``_attempt`` already makes for ``head_sha``, so refusing here costs
        no extra request.

        It mirrors the pre-flight refusal below deliberately: settle at
        zero, then abandon. Zero because no engine ran, which is provable
        rather than assumed; abandon because the answer is deterministic --
        GitHub will not reopen a merged pull request, and another attempt
        would reserve allowance only to read the same state again.
        """
        self.governor.settle(
            claim,
            Usage(0, UsageConfidence.EXACT, engine=self.engine.name),
            now=_now(),
            stop_reason=StopReason.CLOSED,
        )
        self.queue.abandon(claim)
        _log_closed(claim, facts)

    def _reviewed(
        self,
        claim: Claim,
        result: ReviewResult,
        *,
        facts: PullRequestFacts,
        history: PullRequestHistory,
        lines: int,
    ) -> RunEnd:
        """Record what a finished review produced, and say how the run ended."""
        end = RunEnd(
            usage=result.usage,
            reason=_REASON_FOR[result.outcome],
            finish=_finish_for(result.outcome),
        )
        if result.outcome is Outcome.COMPLETED:
            self.completed += 1
            # Numbered here rather than at render time so the numbers are
            # durable: the high-water mark is read back out of this column,
            # and a finding recorded without one would let a retired number
            # come back on something else.
            result = replace(
                result, findings=assign(result.findings, history.high_water)
            )
            end = replace(
                end,
                # Recorded before the settle so the content outlives any
                # failure after it. Only a completed run has publishable
                # findings -- the seam enforces that -- so only one is kept.
                reviewed=self.runs.record(
                    claim.trigger, head_sha=facts.head_sha, result=result, now=_now()
                ),
                # Only a finished review is a sample of what reviewing this
                # many lines costs. A truncated or failed run spent less than
                # a whole one over the same lines, and a usage-limited run
                # settles at *exact* zero -- all three fit a rate lower than
                # the truth, which is the direction that under-refuses.
                reviewed_lines=lines,
            )
        _log_reviewed(claim, result, end.usage)
        return end

    def _failed(self, claim: Claim, exc: Exception, reserved: Usage) -> RunEnd:
        """Say what went wrong in its own vocabulary, and classify the cost.

        The classification is pure and lives in :func:`classify_failure`;
        what is left here is the part that is not -- the log line an operator
        reads, and the breaker an account-wide limit trips.
        """
        key = claim.trigger.dedupe_key
        end = classify_failure(exc, reserved)
        if isinstance(exc, UsageLimited):
            # The wall is the account's, not this run's, so retrying reaches
            # it again having spent to get there. The breaker refuses every
            # claim instead, and the row waits behind it.
            logger.error("%s hit the account's usage limit", key)
            self.governor.trip(_now())
        elif end.finish is Finish.ABANDON:
            logger.error("giving up on %s permanently", key, exc_info=True)
        elif isinstance(exc, EngineUnavailable):
            logger.error(
                "%s could not start %s and will be retried",
                key,
                self.engine.name,
                exc_info=True,
            )
        else:
            logger.error("%s failed and will be retried", key, exc_info=True)
        return end

    async def _review(self, request: ReviewRequest) -> ReviewResult:
        """Run the engine, converting any failure of it into ``EngineError``."""
        try:
            return await self.engine.review(request)
        except UsageLimited:
            # Not an engine failure to retry: it is the account's limit, and
            # `run_one` has an arm of its own for it.
            raise
        except EngineUnavailable:
            # The other failure the flattening must not swallow: the
            # subprocess never started, so `run_one` can settle it at a
            # provable zero rather than at the ceiling.
            raise
        except EngineTimeout as exc:
            raise EngineError(
                f"{self.engine.name} outlived its wall clock: {exc}",
                StopReason.TIMEOUT,
            ) from exc
        except Exception as exc:  # the adapter is a foreign tool; see EngineError
            raise EngineError(
                f"{self.engine.name} failed: {exc}",
                StopReason.ENGINE_ERROR,
                status=exc.status if isinstance(exc, EngineProtocolError) else None,
            ) from exc

    async def _publish_recorded(self, claim: Claim) -> None:
        """Post the run this ``PUBLISH`` item names, and close its row.

        No facts are fetched, no checkout is made, no engine is reached and
        -- because :meth:`admit` let this claim through without reserving --
        there is nothing on the ledger to settle. The item costs nothing and
        writes nothing, which is the honest record of it.

        It is an ordinary queue row, so it takes the ordinary per-pull-request
        lease: one pull request stays in one worker's hands, and a second
        lease would be a second chance to post the same comment twice. The
        lease is re-checked immediately before the write, because the owner
        guards on ``complete`` and ``release`` only run *after* it -- late
        enough to discard a row, too late to unsay a comment.

        A failure hands the row back **unattempted**. The attempt bound caps
        what one poison trigger may drain, and a post that reached no engine
        drained nothing; counting it would abandon a review after three
        failed posts and leave its findings recorded and permanently
        invisible.

        It is counted on the *run* instead, against
        ``publish.max_publish_attempts``. A post GitHub will never accept --
        a locked pull request, a repository whose issues were turned off --
        was otherwise retried on every claim for the lifetime of the
        database, which is the gap ``STATUS.md`` used to record. At the
        bound the run is stamped, this item is closed rather than handed
        back, and an ERROR names what has to be looked at.
        """
        key = published_run_key(claim.trigger)
        pending = self.runs.unpublished(key)
        if pending is None:
            # Posted, purged, or never recorded -- see `RunStore.unpublished`.
            # Nothing to do and nothing to retry, so the item is finished.
            logger.info("%s needs no publishing; closing the item", key)
            self.queue.complete(claim)
            return
        logger.info("%s was reviewed already; publishing without a second review", key)
        if not self.queue.holds(claim, now=_now()):
            logger.warning("lease lapsed before %s republished", key)
            return
        # ``_gave_up`` runs only when the post failed, and closes the item
        # rather than handing it back when that failure was the last one
        # allowed.
        if await self._published(claim, pending) or self._gave_up(pending):
            self.queue.complete(claim)
        else:
            self.queue.release_unattempted(claim)

    def _gave_up(self, run: RecordedRun) -> bool:
        """Count a failed post against ``run``; ``True`` once it is given up.

        The limit is read through the publisher because ``publish`` is one
        of the two reloadable sections: an operator who raises it over
        ``SIGHUP`` should not have to restart the daemon to have the next
        retry honour it.
        """
        if not self.runs.publish_failed(
            run.dedupe_key,
            limit=self.publisher.config.max_publish_attempts,
            now=_now(),
        ):
            return False
        logger.error(
            "gave up posting %s on %s#%d after %d attempts; the review is "
            "recorded and will not be offered again until publish_failed_at "
            "is cleared on that row",
            run.dedupe_key,
            run.repo,
            run.pr_number,
            self.publisher.config.max_publish_attempts,
            extra={"repo": run.repo, "pr": run.pr_number},
        )
        return True

    async def _settle_publish_and_finish(self, claim: Claim, end: RunEnd) -> None:
        """Record what the run cost, post it, then close its row.

        All three are guarded on the owner. A worker whose lease lapsed
        mid-run gets ``False`` from ``settle`` and stops there, which is how
        it learns to discard a result it is no longer entitled to publish --
        now with teeth, because the thing being discarded is a comment under
        the agent's own account.

        ``end.reviewed_lines`` is what the pre-flight estimate is fitted
        against, and it is ``None`` for every run that did not finish a
        review.
        """
        if not self.governor.settle(
            claim,
            end.usage,
            now=_now(),
            stop_reason=end.reason,
            reviewed_lines=end.reviewed_lines,
        ):
            logger.warning(
                "no reservation to settle for %s: discarding the run",
                claim.trigger.dedupe_key,
            )
            return
        # The remaining allowance, once per review rather than once per poll
        # cycle. Read *after* the settle on purpose: until then the ledger
        # still holds this run's reservation at `max_run_tokens`, so the
        # number would understate what is left by whatever the run did not
        # spend. `headroom` is already public and already actor-agnostic --
        # its docstring names this use.
        headroom = self.governor.headroom(_now())
        logger.info(
            "budget after %s: %d tokens left in the %s window, mode=%s",
            claim.trigger.dedupe_key,
            headroom.remaining,
            headroom.tightest,
            headroom.mode,
            extra={
                "remaining": headroom.remaining,
                "tightest": headroom.tightest,
                "mode": headroom.mode,
            },
        )
        reviewed = end.reviewed
        # The review itself is done and must not be run again, so its row is
        # completed and what remains -- the posting -- is enqueued as work of
        # its own. Handing *this* row back instead would make the next claim
        # for this pull request a republication wearing a review trigger's
        # name. The first post counts like every later one: enqueueing an
        # item for a run already given up on would only produce a claim that
        # finds nothing to do.
        if (
            reviewed is not None
            and not await self._published(claim, reviewed)
            and not self._gave_up(reviewed)
        ):
            self.queue.enqueue(publication_of(claim.trigger, reviewed), now=_now())
        self._verb(end.finish)(claim)

    def _verb(self, finish: Finish) -> Callable[[Claim], bool]:
        """The queue method that closes a row this way."""
        return {
            Finish.COMPLETE: self.queue.complete,
            Finish.RELEASE: self.queue.release,
            Finish.RELEASE_UNATTEMPTED: self.queue.release_unattempted,
            Finish.ABANDON: self.queue.abandon,
        }[finish]

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
            logger.error(
                "could not publish %s; the review is kept and will be "
                "posted without being run again",
                key,
                exc_info=True,
            )
            return False
        if published.outcome is PublishOutcome.SUPERSEDED:
            logger.info("%s was superseded before it could be posted", key)
        return True
