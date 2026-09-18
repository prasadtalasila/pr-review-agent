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

#: Rendering order. ``Severity`` declares the levels but not their gravity,
#: and iteration order over an enum is a definition detail rather than a
#: promise -- so the order a reader sees is pinned here, where a test can
#: read it. Stable ordering is also what makes an edit-in-place a no-op diff
#: when a re-review finds the same things.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.BLOCKER,
    Severity.MAJOR,
    Severity.MINOR,
    Severity.NIT,
)

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
            logger.warning(
                "could not acknowledge %s", trigger.dedupe_key, exc_info=True
            )

    async def publish(self, run: RecordedRun) -> Published:
        """Post ``run``'s review, unless the head moved under it.

        The live read comes first and unconditionally, including in a dry
        run: an operator watching a dry run needs to see the same decision
        the real path would take, not a shortcut past it.
        """
        live = await self._live_head(run.pr_number)
        if live != run.head_sha:
            logger.info(
                "%s reviewed %s but the head is now %s: discarding",
                run.dedupe_key,
                run.head_sha[:7],
                live[:7],
            )
            return Published(PublishOutcome.SUPERSEDED)

        body = render(run.head_sha, run.findings)
        if self.config.dry_run:
            logger.info(
                "publish.dry_run: not posting on %s#%d:\n%s",
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
        )
        return self._stamp(run, comment_id=comment_id)

    async def _live_head(self, pr_number: int) -> str:
        """The head this pull request has *now*, read fresh every time."""
        result = await self.client.get(self.endpoints.pull(pr_number))
        try:
            return str(result.data["head"]["sha"])  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise PayloadError(
                f"pull request {pr_number} reported no head sha"
            ) from exc

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


def render(head_sha: str, findings: tuple[Finding, ...]) -> str:
    """The comment body for a review of ``head_sha``.

    The commit is named because the comment is edited in place: without it a
    reader cannot tell which revision the text describes, and an edit that
    silently replaces a review of an older commit is the one way this
    design can mislead.

    Finding bodies are engine output over an untrusted tree, and are written
    through verbatim. They are rendered as Markdown by GitHub inside the
    agent's own comment, which is the same trust boundary any human comment
    has -- what keeps them harmless is that this module can take no action
    they could ask for.
    """
    header = f"### Review of `{head_sha[:7]}`"
    if not findings:
        return f"{header}\n\nNo issues found.\n\n{TRAILER}"
    lines = "\n".join(
        f"- **{finding.severity}** `{finding.path}:{finding.line}` — {finding.body}"
        for finding in sorted(findings, key=_order)
    )
    return f"{header}\n\n{lines}\n\n{TRAILER}"


def _order(finding: Finding) -> tuple[int, str, int]:
    """Severity first, then location -- so the same findings render the same."""
    return (SEVERITY_ORDER.index(finding.severity), finding.path, finding.line)
