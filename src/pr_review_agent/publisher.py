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
from .engine import Finding, Severity
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import RepoEndpoints
from .runs import RecordedRun, RunStore
from .sanitise import leaks, sanitise
from .triggers.mention import neutralise
from .triggers.models import PayloadError, Trigger

logger = logging.getLogger(__name__)

#: GitHub's name for 👀. The only reaction this agent ever posts.
EYES = "eyes"

#: Which heading each severity renders under, in the order a reader sees
#: them. Pinned here, where a test can read it, because iteration order over
#: an enum is a definition detail rather than a promise -- and because a
#: reader comparing this round's comment with the last one is reading a
#: diff, and only a pinned order makes the things that changed the things
#: that stand out.
#:
#: ``major`` and ``minor`` share a heading on purpose. ``Severity`` is
#: persisted and asserted across the suite, so it is not collapsed to three
#: values; but a ``major`` finding that is not a blocker must not be printed
#: under a heading claiming it blocks.
SECTIONS: tuple[tuple[str, tuple[Severity, ...]], ...] = (
    ("Blocking", (Severity.BLOCKER,)),
    ("Should fix", (Severity.MAJOR, Severity.MINOR)),
    ("Nits", (Severity.NIT,)),
)

#: Severity to its rank, derived from ``SECTIONS`` so the two cannot drift.
_RANK: dict[Severity, int] = {
    severity: index
    for index, (_, severities) in enumerate(SECTIONS)
    for severity in severities
}

#: Said once, on every comment. An automated remark that reads like a
#: verdict invites being treated as one, and this agent's opinion is
#: deliberately not one.
TRAILER = (
    "<sub>Automated review. It takes no action on this pull request "
    "beyond this comment.</sub>"
)

#: Said at the end of every refusal notice. Both halves are load-bearing:
#: a contributor who has just been refused wants to know whether it cost
#: anything, and what to do next -- and the answer to the second is never
#: "wait", because a deterministic refusal ends the trigger for good.
REFUSAL_TRAILER = (
    "<sub>No review ran and nothing was charged. Once the pull request or "
    "the configuration has changed, a new @{handle} comment asks again.</sub>"
)

#: How long a rendered body may be. GitHub rejects a comment over 65 536
#: characters with a 422 -- *after* the review was paid for -- and the
#: worker would then re-offer the same body on every claim forever. The
#: margin absorbs the header, the trailer and the entities escaping adds.
MAX_BODY_CHARS = 60_000

#: Said in place of the sections that did not fit, so a reader is never
#: shown a partial review that looks complete.
TRUNCATION_NOTE = (
    "_Some findings were omitted: this review did not fit a GitHub comment. "
    "The lowest-severity sections were dropped first._"
)


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
        if self.config.dry_run:
            logger.info(
                "publish.dry_run: not posting a refusal notice on %s#%d: %s",
                trigger.repo,
                trigger.pr_number,
                notice,
            )
            return
        body = refusal(notice, handle=self.handle)
        try:
            response = await self.client.post(
                self.endpoints.issue_comments(trigger.pr_number), {"body": body}
            )
        except GitHubClientError:
            logger.error(
                "could not post the refusal notice for %s",
                trigger.dedupe_key,
                exc_info=True,
            )
            return
        comment_id = int(response["id"])
        self.posted.record(trigger.repo, comment_id, now=datetime.now(timezone.utc))
        logger.info(
            "refused %s on %s#%d as comment %d",
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


def render(
    head_sha: str,
    findings: tuple[Finding, ...],
    *,
    pr_number: int,
    round_number: int,
    commits: int,
    handle: str,
    moved_to: str | None = None,
) -> str:
    """The comment body for a review of ``head_sha``.

    The commit is named because a pull request under review has several,
    and a reader scrolling past two comments from the agent has to be able
    to tell which revision each of them describes. The round and the commit
    count are there for the same reason -- "round 3" and "round 1" are
    different statements, including when both found nothing.

    A finding renders no ``path:line`` anchor. The paths that matter are the
    ones the reviewer names in its own prose, which is what the reference
    report this template was drawn from does; an anchor beside every headline
    reads as machine output and crowds the sentence meant to be read first.
    The location is still on the stored ``Finding``, where a future
    line-anchored comment would need it.

    ``handle`` is required rather than defaulted. ``"claude"`` is already
    spelled as the default of ``TriggerConfig.handle``, and a second copy
    here is a copy that can drift -- in the one direction where drift means
    the agent summons itself.

    Finding titles and bodies are engine output over an untrusted tree, so
    every one of them goes through ``sanitise`` before it is assembled:
    what a reader sees is unchanged, and what GitHub would have *done* with
    it -- notify an account, cross-reference an issue, render HTML -- it no
    longer does. The trailer is the only HTML in the result, and it is ours.
    See ``docs/reporting/review-report.md`` for the contract this
    implements.
    """
    header = (
        f"## Review: PR #{pr_number} — round {round_number} "
        f"(`{head_sha[:7]}`, {commits} commits)"
    )
    if moved_to is not None:
        header = f"{header}\n\n{_moved_note(head_sha, moved_to)}"
    if not findings:
        return neutralise(f"{header}\n\nNo issues found.\n\n{TRAILER}", handle)
    ordered = sorted(findings, key=_order)
    sections = []
    for heading, severities in SECTIONS:
        section = [f for f in ordered if f.severity in severities]
        if not section:
            continue
        rendered = _prose(section) if heading == "Nits" else _items(section)
        sections.append(f"## {heading}\n\n{rendered}")
    return neutralise(_fit(header, sections), handle)


def refusal(notice: str, *, handle: str) -> str:
    """Assemble one deterministic refusal into a comment body.

    Pure, and short enough that the length cap :func:`render` works around
    cannot be reached: every notice is fixed text over a handful of
    integers and configuration key names this process chose itself.

    It still goes through ``neutralise``, which is not ceremony. The
    trailer tells the reader to comment ``@handle`` again, and a comment
    the agent posts is a comment the poller reads back -- so without it the
    advice would summon the review it is explaining the absence of, on a
    pull request already known to be unreviewable. What ``sanitise`` adds
    for a review is left out, because there is no engine prose here for it
    to escape.
    """
    return neutralise(
        f"**Not reviewed.** {notice}\n\n{REFUSAL_TRAILER.format(handle=handle)}",
        handle,
    )


def _moved_note(head_sha: str, moved_to: str) -> str:
    """Said above a review whose commit is no longer the head.

    The tokens were spent before the head was re-read, so the choice is
    between a review nobody sees and one that says what it describes. Most
    of a review survives a fixup commit, and the note is what keeps a reader
    who finds this comment later from taking it for a review of the head.
    """
    return (
        f"_This review describes `{head_sha[:7]}`, which is no longer the head: "
        f"the branch has since moved to `{moved_to[:7]}`. Findings may already "
        "be addressed. Mention me again for a review of the new head._"
    )


def _fit(header: str, sections: list[str]) -> str:
    """The body, trimmed to ``MAX_BODY_CHARS`` if it does not fit.

    Whole sections go first, lowest severity first, because ``SECTIONS`` is
    already in descending order of how much the reader needs them -- so the
    cut is principled rather than wherever the character count landed. Only
    when the *highest*-severity section alone is over the limit is prose cut
    mid-sentence, and it is still marked.
    """
    kept = list(sections)
    while True:
        body = _assemble(header, kept, truncated=len(kept) < len(sections))
        if len(body) <= MAX_BODY_CHARS:
            return body
        if len(kept) == 1:
            break
        kept.pop()
    room = MAX_BODY_CHARS - len(_assemble(header, [""], truncated=True))
    return _assemble(header, [kept[0][: max(room, 0)].rstrip()], truncated=True)


def _assemble(header: str, sections: list[str], *, truncated: bool) -> str:
    """Header, sections, the truncation note if one is owed, then the trailer."""
    parts = [header, *sections]
    if truncated:
        parts.append(TRUNCATION_NOTE)
    parts.append(TRAILER)
    return "\n\n".join(parts)


def _items(findings: list[Finding]) -> str:
    """Numbered entries: bold headline, then the body indented beneath it."""
    return "\n\n".join(
        f"{finding.number}. **{sanitise(finding.title)}**\n\n"
        f"{_indent(sanitise(finding.body))}"
        for finding in findings
    )


def _prose(findings: list[Finding]) -> str:
    """Nits, run together as sentences. One that needs an entry is not a nit."""
    return " ".join(
        sanitise(f"{finding.title} {finding.body}".strip()) for finding in findings
    )


def _indent(body: str) -> str:
    """Indent a body under its numbered entry, leaving blank lines blank."""
    return "\n".join(f"   {line}" if line.strip() else "" for line in body.splitlines())


def _order(finding: Finding) -> tuple[int, int, str, int]:
    """Section, then number, then location -- so the same findings render the same.

    ``number`` sorts before location so a report's entries ascend, and a
    finding is numbered before it is recorded, so ``None`` never reaches here
    on a published run. It is tolerated rather than asserted because a header
    is not worth failing a publish over.
    """
    return (
        _RANK[finding.severity],
        finding.number if finding.number is not None else 0,
        finding.path,
        finding.line,
    )
