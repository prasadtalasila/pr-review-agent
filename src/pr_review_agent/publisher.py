"""The last step: make a computed review visible.

Two things happen here, minutes apart, and the gap between them is the point.

**The acknowledgement is immediate.** A 👀 goes on whatever the contributor
touched -- the comment they typed the handle into, or the pull request itself
when nobody typed anything -- as soon as the trigger is claimed, long before
a review exists. It is what makes an adaptive 10-600 s poll interval feel
like an answer rather than a silence, and it is why ``DESIGN.md`` could
reject an internet-facing relay for a few seconds of latency. It never
raises: losing a courtesy must not cost a review the governor has already
reserved allowance for.

**The publication is late, and re-reads the head first.** A review describes
one commit. By the time it finishes, the pull request may have moved on, and
a review of a superseded commit landing late is worse than no review --
``QUEUE.md`` puts the check here deliberately, because only a read taken
immediately before posting is worth anything.

**One comment per review, posted afresh.** Each review posts its own
ordinary issue comment and edits nothing; the id it gets is recorded against
the run and never written to again. The agent kept one comment per pull
request and rewrote it until 1.3.0, which read well but made the review's
last step depend on a comment anybody could delete: a maintainer tidying a
thread left an id that answered 404 forever, and a paid review was retried
invisibly on every claim (issue #71). A comment that cannot be edited cannot
be lost that way, and each reviewed commit keeps a durable, linkable comment
of its own. What bounds the thread is not the edit but
:mod:`pr_review_agent.pacing`, which collapses a burst of triggers into one
review, and ``ReviewQueue.fold``, which answers every trigger waiting on a
pull request with the one review it ran.

**Nothing it posts can summon another review.** A review body is engine
prose over an untrusted tree, and when the tree is this repository that prose
readily contains ``@claude``, and a comment the agent posts is a comment the
poller reads back. So ``render`` runs every body through
``triggers.mention.neutralise``, which rewrites exactly the mentions the
classifier would find into ``&#64;`` -- a
reader still sees ``@claude``, and the raw body a later poll reads back has
no ``@`` for ``has_mention`` to match. This is where the loop is closed, and
it is why the classifier needs no notion of who the agent is.

**Nothing it posts can act.** Closing the self-mention loop was the first
half of that; ``sanitise`` is the rest. A finding is model prose over an
untrusted tree, and GitHub lets ordinary comment text notify an account,
cross-reference an issue and render HTML -- all attributed to the agent. So
every engine-authored title and body is escaped before assembly, the
rendered body is capped below the limit GitHub rejects a comment at, and a
body carrying a known secret is refused rather than posted. See
:mod:`pr_review_agent.sanitise`.

**This module cannot approve anything.** ``DESIGN.md`` names three
prompt-injection mitigations and this is the third: whatever a review
concludes, the agent takes no approval action and no merge action. That is
held here by an *absent capability* rather than a guarded field -- the only
GitHub writes this module knows how to make are a reaction and an ordinary
issue comment. There is no field to set wrongly and no branch to get wrong,
so a pull request whose text asks to be approved cannot get what it asks for
even if every other mitigation fails and the model does exactly as it is
told. A test asserts the source contains no path or token that could change
that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ._compat import StrEnum
from .comments import AgentComments
from .config import PublishConfig
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import RepoEndpoints

# Re-exported so that ``from .publisher import TRAILER`` keeps working --
# the suite, the skill's checker and the docs all spell it that way, and
# the split below is about what depends on httpx, not about renaming.
from .report import MAX_BODY_CHARS as MAX_BODY_CHARS
from .report import SECTIONS as SECTIONS
from .report import TRAILER as TRAILER
from .report import TRUNCATION_NOTE as TRUNCATION_NOTE
from .report import failure, refusal, render
from .runs import RecordedRun, RunStore
from .sanitise import leaks
from .triggers.models import PayloadError, Trigger

logger = logging.getLogger(__name__)

#: GitHub's name for 👀. The only reaction this agent ever posts.
EYES = "eyes"


class PublishOutcome(StrEnum):
    """How an attempt to publish ended.

    ``SUPERSEDED`` says the head moved under the review. Whether anything
    was posted under it depends on ``publish.post_superseded``, and that is
    deliberate: the outcome records what was *true of the run*, not which
    branch the publisher took, so a stored row means the same thing after an
    operator changes the setting.
    """

    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    DRY_RUN = "dry_run"
    #: The body carried a credential, so nothing was posted and nothing
    #: will be: re-running would produce the same body at the same price.
    REFUSED = "refused"


@dataclass(frozen=True)
class Published:
    """What a publish attempt did, and which comment it left behind."""

    outcome: PublishOutcome
    comment_id: int | None = None


@dataclass
class Publisher:
    """Acknowledges a claim, and posts the review it eventually produces."""

    client: GitHubClient
    endpoints: RepoEndpoints
    runs: RunStore
    #: Every comment this posts, so the classifier can tell the agent's own
    #: words from a contributor's. Written here rather than in ``runs``
    #: because it has to cover any comment the publisher learns to post,
    #: not only the ones a review is behind.
    posted: AgentComments
    config: PublishConfig
    #: The handle ``render`` must not leave in anything it posts. Set once at
    #: construction rather than through ``reload``: ``Daemon.reload_config``
    #: reloads only ``budget`` and ``publish``, and warns that a change to
    #: ``triggers`` needs a restart -- so the handle cannot move under a
    #: running process.
    handle: str
    #: Values that must never appear in a posted body -- the GitHub token,
    #: today. Empty by default so a test constructing a publisher need not
    #: invent one; the daemon passes the live token.
    secrets: tuple[str, ...] = ()

    def reload(self, config: PublishConfig) -> None:
        """Adopt ``config``, so ``SIGHUP`` needs no restart to take effect."""
        self.config = config

    async def acknowledge(self, trigger: Trigger) -> None:
        """Post 👀 where the contributor will see it. Never raises.

        A mention is acknowledged on the comment it was written in, which
        needs both the id and which endpoint it came from -- the two are
        drawn from different sequences. A freshly opened pull request has no
        such comment, so the reaction goes on the pull request itself.

        ``dry_run`` does not suppress this. The reaction says "the agent has
        your trigger", which is true in a dry run and is the one thing an
        operator watching a dry run still wants a contributor to see.
        """
        if trigger.comment_id is not None and trigger.comment_source is not None:
            path = self.endpoints.comment_reactions(
                trigger.comment_id, trigger.comment_source
            )
        else:
            path = self.endpoints.issue_reactions(trigger.pr_number)
        try:
            await self.client.post(path, {"content": EYES})
        except GitHubClientError:
            # Narrow on purpose. A GitHub failure is not worth a review the
            # governor has already reserved allowance for; a bug in this
            # module is, and swallowing every exception would hide one
            # behind a missing emoji.
            logger.error("could not acknowledge %s", trigger.dedupe_key, exc_info=True)

    async def notify(self, trigger: Trigger, notice: str) -> None:
        """Say that a deterministic refusal ended this trigger. Never raises.

        The 👀 goes on as soon as the claim is made, minutes before anyone
        knows whether a review is possible. When the size gate or the
        pre-flight then refuses, the row is abandoned and that is the whole
        of the contributor's experience: an acknowledgement and then
        silence, with the reason visible only in the operator's journal
        (issue #78). A maintainer who typed the handle on a 6 000-line pull
        request had no way to learn that raising a cap is the fix.

        **Only deterministic refusals are announced.** A transient failure
        is retried and will answer for itself; a closed pull request is not
        told anything, because nobody is reading it; and a ``PayloadError``
        is a bug in the agent rather than something the pull request can
        act on. What is left is the set a contributor or maintainer can
        actually do something about, which is what makes a notice worth the
        comment it costs.

        **It cannot accumulate.** One notice ends one trigger, and the row
        is abandoned in the same breath, so nothing re-offers it. A second
        notice means a second deliberate ``@handle`` -- somebody asking
        again -- and answering that one too is the point rather than a
        leak.

        Suppressed by ``dry_run``, unlike :meth:`acknowledge`. The reaction
        says "your trigger arrived", which is true in a dry run; this
        writes a comment under the agent's account, which is exactly what
        the brake is on to prevent.

        Never raises, for the same reason the acknowledgement does not: the
        row this explains is already closed and settled at zero, and
        letting a failed courtesy propagate would turn a free refusal into
        a retried failure that reserves allowance to reach the same answer.
        """
        await self._post_notice(
            trigger, notice, refusal(notice, handle=self.handle), kind="refusal"
        )

    async def report_failure(
        self, trigger: Trigger, notice: str, *, attempts: int
    ) -> None:
        """Say that every attempt this trigger was allowed has failed.

        The one announced ending that is not deterministic, which is why it
        waits for the last attempt: before that, the retry answers for
        itself. It cannot accumulate either -- the row has no attempts left,
        so nothing claims it again. Never raises, and suppressed by
        ``dry_run``, for the reasons :meth:`notify` gives.
        """
        body = failure(notice, handle=self.handle, attempts=attempts)
        await self._post_notice(trigger, notice, body, kind="failure")

    async def _post_notice(
        self, trigger: Trigger, notice: str, body: str, *, kind: str
    ) -> None:
        """Post one notice on the pull request, and never raise."""
        if self.config.dry_run:
            logger.info(
                "publish.dry_run: not posting a %s notice on %s#%d: %s",
                kind,
                trigger.repo,
                trigger.pr_number,
                notice,
            )
            return
        try:
            response = await self.client.post(
                self.endpoints.issue_comments(trigger.pr_number), {"body": body}
            )
        except GitHubClientError:
            logger.error(
                "could not post the %s notice for %s",
                kind,
                trigger.dedupe_key,
                exc_info=True,
            )
            return
        comment_id = int(response["id"])
        self.posted.record(trigger.repo, comment_id, now=datetime.now(timezone.utc))
        logger.info(
            "posted a %s notice for %s on %s#%d as comment %d",
            kind,
            trigger.dedupe_key,
            trigger.repo,
            trigger.pr_number,
            comment_id,
            extra={
                "repo": trigger.repo,
                "pr": trigger.pr_number,
                "comment": comment_id,
            },
        )

    async def publish(self, run: RecordedRun) -> Published:
        """Post ``run``'s review, saying so if the head moved under it.

        The live read comes first and unconditionally, including in a dry
        run: an operator watching a dry run needs to see the same decision
        the real path would take, not a shortcut past it.

        A moved head no longer discards the review by default. The tokens
        were spent before this method was called, so discarding saves
        nothing and produces nothing, and most of a review survives a fixup
        commit. What the reader needs is to know which commit the text
        describes, which the header says. Since 1.3.0 this comment is not
        replaced by the next round -- each review posts its own -- so the
        note stays in the thread beside the review of the new head, which is
        the honest record of what was reviewed and when.
        ``publish.post_superseded: false`` restores the old behaviour, and
        stamps the run either way -- an unstamped run is offered for
        publication for the lifetime of the database.
        """
        live, commits = await self._live_pull(run.pr_number)
        moved = live if live != run.head_sha else None
        if moved is not None:
            logger.info(
                "%s reviewed %s but the head is now %s: %s",
                run.dedupe_key,
                run.head_sha[:7],
                moved[:7],
                "saying so on the comment"
                if self.config.post_superseded
                else "discarding",
            )
            if not self.config.post_superseded:
                return self._stamp(
                    run, comment_id=None, outcome=PublishOutcome.SUPERSEDED
                )

        body = render(
            run.head_sha,
            run.findings,
            pr_number=run.pr_number,
            round_number=self.runs.round_of(run.repo, run.pr_number, run.dedupe_key),
            commits=commits,
            handle=self.handle,
            moved_to=moved,
            omitted=run.omitted,
            assessment=run.assessment,
        )
        if leaks(body, self.secrets):
            # Not retried, and not logged with the body: re-running would
            # spend again to produce the same comment, and an ERROR that
            # quotes the leak is a second copy of it.
            logger.error(
                "%s rendered a review containing a credential; refusing to post it",
                run.dedupe_key,
            )
            return self._stamp(run, comment_id=None, outcome=PublishOutcome.REFUSED)
        if self.config.dry_run:
            # The whole rendered review body on every run is a DEBUG-sized
            # record, so INFO keeps only the one line saying it happened --
            # which is what tells an operator the brake is on at all.
            logger.info(
                "publish.dry_run: not posting on %s#%d", run.repo, run.pr_number
            )
            logger.debug(
                "publish.dry_run: the body not posted on %s#%d:\n%s",
                run.repo,
                run.pr_number,
                body,
            )
            return self._stamp(run, comment_id=None, outcome=PublishOutcome.DRY_RUN)

        response = await self.client.post(
            self.endpoints.issue_comments(run.pr_number), {"body": body}
        )
        comment_id = int(response["id"])
        # Before the run is stamped: a crash between the two leaves a
        # comment the agent will not answer, where the other order leaves
        # one it might.
        self.posted.record(run.repo, comment_id, now=datetime.now(timezone.utc))
        logger.info(
            "published %s on %s#%d as comment %d",
            run.dedupe_key,
            run.repo,
            run.pr_number,
            comment_id,
            extra={"repo": run.repo, "pr": run.pr_number, "comment": comment_id},
        )
        return self._stamp(
            run,
            comment_id=comment_id,
            outcome=(PublishOutcome.SUPERSEDED if moved else PublishOutcome.PUBLISHED),
        )

    async def _live_pull(self, pr_number: int) -> tuple[str, int]:
        """The head and commit count this pull request has *now*.

        Both come off the read the publisher already makes, so the header's
        commit count costs no second round trip and needs no column. A
        missing or non-integer ``commits`` reads as ``0`` rather than
        raising: a header is not worth failing a publish over, and the head
        sha -- which decides whether to publish at all -- is still required.
        """
        result = await self.client.get(self.endpoints.pull(pr_number))
        try:
            head = str(result.data["head"]["sha"])  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise PayloadError(
                f"pull request {pr_number} reported no head sha"
            ) from exc
        commits = result.data.get("commits")  # type: ignore[union-attr]
        return head, commits if isinstance(commits, int) else 0

    def _stamp(
        self,
        run: RecordedRun,
        *,
        comment_id: int | None,
        outcome: PublishOutcome = PublishOutcome.PUBLISHED,
    ) -> Published:
        """Record that this run needs publishing no longer.

        A dry run is stamped too. The pipeline ran and there is nothing left
        to post, so leaving it unstamped would make every claim re-offer it
        for the lifetime of the database.
        """
        self.runs.mark_published(
            run.dedupe_key,
            comment_id=comment_id,
            now=datetime.now(timezone.utc),
            outcome=str(outcome),
        )
        return Published(outcome, comment_id)
