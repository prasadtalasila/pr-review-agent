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

**One comment per pull request, rewritten in place.** A re-review edits the
comment the agent already has rather than adding another, which is what
stops a thread filling with superseded machine opinion. The history lives in
``runs`` and the ledger, where it can be queried and purged, rather than in
a comment thread where it can only be scrolled past.

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
from .config import PublishConfig
from .engine import Finding, Severity
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import RepoEndpoints
from .runs import RecordedRun, RunStore
from .triggers.models import PayloadError, Trigger

logger = logging.getLogger(__name__)

#: GitHub's name for 👀. The only reaction this agent ever posts.
EYES = "eyes"

#: Which heading each severity renders under, in the order a reader sees
#: them. Pinned here, where a test can read it, because iteration order over
#: an enum is a definition detail rather than a promise -- and because stable
#: ordering is what makes an edit-in-place a no-op diff when a re-review
#: finds the same things.
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


class PublishOutcome(StrEnum):
    """How an attempt to publish ended.

    ``SUPERSEDED`` and ``DRY_RUN`` are both "nothing was posted, and that is
    correct" -- but they are kept apart because only one of them means the
    work was wasted. Collapsing them would hide a moved head behind an
    operator's own setting.
    """

    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    DRY_RUN = "dry_run"


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
    config: PublishConfig

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

    async def publish(self, run: RecordedRun) -> Published:
        """Post ``run``'s review, unless the head moved under it.

        The live read comes first and unconditionally, including in a dry
        run: an operator watching a dry run needs to see the same decision
        the real path would take, not a shortcut past it.
        """
        live, commits = await self._live_pull(run.pr_number)
        if live != run.head_sha:
            logger.info(
                "%s reviewed %s but the head is now %s: discarding",
                run.dedupe_key,
                run.head_sha[:7],
                live[:7],
            )
            return Published(PublishOutcome.SUPERSEDED)

        body = render(
            run.head_sha,
            run.findings,
            pr_number=run.pr_number,
            round_number=self.runs.round_of(run.repo, run.pr_number, run.dedupe_key),
            commits=commits,
        )
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

        existing = self.runs.comment_for_pull_request(run.repo, run.pr_number)
        if existing is None:
            posted = await self.client.post(
                self.endpoints.issue_comments(run.pr_number), {"body": body}
            )
        else:
            posted = await self.client.patch(
                self.endpoints.issue_comment(existing), {"body": body}
            )
        comment_id = int(posted["id"])
        logger.info(
            "published %s on %s#%d as comment %d",
            run.dedupe_key,
            run.repo,
            run.pr_number,
            comment_id,
            extra={"repo": run.repo, "pr": run.pr_number, "comment": comment_id},
        )
        return self._stamp(run, comment_id=comment_id)

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
            run.dedupe_key, comment_id=comment_id, now=datetime.now(timezone.utc)
        )
        return Published(outcome, comment_id)


def render(
    head_sha: str,
    findings: tuple[Finding, ...],
    *,
    pr_number: int,
    round_number: int,
    commits: int,
) -> str:
    """The comment body for a review of ``head_sha``.

    The commit is named because the comment is edited in place: without it a
    reader cannot tell which revision the text describes, and an edit that
    silently replaces a review of an older commit is the one way this design
    can mislead. The round and the commit count are there for the same
    reason -- "round 3" and "round 1" are different statements, including
    when both found nothing.

    A finding renders no ``path:line`` anchor. The paths that matter are the
    ones the reviewer names in its own prose, which is what the reference
    report this template was drawn from does; an anchor beside every headline
    reads as machine output and crowds the sentence meant to be read first.
    The location is still on the stored ``Finding``, where a future
    line-anchored comment would need it.

    Finding titles and bodies are engine output over an untrusted tree, and
    are written through verbatim. They are rendered as Markdown by GitHub
    inside the agent's own comment, which is the same trust boundary any
    human comment has -- what keeps them harmless is that this module can
    take no action they could ask for. See
    ``docs/reporting/review-report.md`` for the contract this implements.
    """
    header = (
        f"## Review: PR #{pr_number} — round {round_number} "
        f"(`{head_sha[:7]}`, {commits} commits)"
    )
    if not findings:
        return f"{header}\n\nNo issues found.\n\n{TRAILER}"
    ordered = sorted(findings, key=_order)
    parts = [header]
    for heading, severities in SECTIONS:
        section = [f for f in ordered if f.severity in severities]
        if not section:
            continue
        parts.append(f"## {heading}")
        parts.append(_prose(section) if heading == "Nits" else _items(section))
    parts.append(TRAILER)
    return "\n\n".join(parts)


def _items(findings: list[Finding]) -> str:
    """Numbered entries: bold headline, then the body indented beneath it."""
    return "\n\n".join(
        f"{finding.number}. **{finding.title}**\n\n{_indent(finding.body)}"
        for finding in findings
    )


def _prose(findings: list[Finding]) -> str:
    """Nits, run together as sentences. One that needs an entry is not a nit."""
    return " ".join(f"{finding.title} {finding.body}".strip() for finding in findings)


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
