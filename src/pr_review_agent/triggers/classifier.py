"""Decide whether a polled event may start a review.

Exactly two events qualify: a freshly opened pull request by an allowlisted
author, and a comment mentioning the agent from an allowlisted commenter.
Pushes to an existing pull request are deliberately ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .allowlist import Allowlist
from .mention import has_mention
from .models import Actor, Comment, Decision, PullRequest, Trigger, TriggerKind

logger = logging.getLogger(__name__)

#: Reasons that fire on essentially every poll: ``not_fresh`` once per
#: already-open pull request, ``no_mention`` once per comment. They stay at
#: ``DEBUG`` so the operator-relevant rejections are readable at ``INFO``.
NOISY_REASONS = frozenset({"not_fresh", "no_mention"})


@dataclass(frozen=True)
class Classifier:
    """Applies eligibility rules to polled pull requests and comments.

    ``since`` is the cold-start watermark. The poller sees *open* pull
    requests rather than ``opened`` webhook events, so without it the first
    poll would treat every already-open pull request as fresh and review the
    whole backlog at once.
    """

    allowlist: Allowlist
    since: datetime
    agent_user_id: int | None = None
    handle: str = "claude"

    def __post_init__(self) -> None:
        """Reject a naive watermark.

        GitHub timestamps are aware UTC (``...Z``). Comparing one against a
        naive ``since`` raises ``TypeError`` on the first pull request seen,
        which is the worst possible time to find out.
        """
        if self.since.tzinfo is None or self.since.utcoffset() is None:
            raise ValueError("Classifier.since must be timezone-aware (UTC)")

    def _is_self(self, actor: Actor) -> bool:
        return self.agent_user_id is not None and actor.user_id == self.agent_user_id

    def classify_pull_request(self, pr: PullRequest) -> Decision:
        """Accept a freshly opened pull request from an allowlisted author."""
        decision = self._decide_pull_request(pr)
        self._log(decision, repo=pr.repo, pr_number=pr.number, kind="pr_opened")
        return decision

    def _decide_pull_request(self, pr: PullRequest) -> Decision:
        if self._is_self(pr.author):
            return Decision(None, "self_author")
        if pr.author.is_bot:
            return Decision(None, "bot_author")
        if pr.is_draft:
            return Decision(None, "draft")
        if pr.created_at <= self.since:
            return Decision(None, "not_fresh")
        if not self.allowlist.allows(pr.author):
            return Decision(None, "author_not_allowlisted")
        return Decision(
            Trigger(
                kind=TriggerKind.PR_OPENED,
                repo=pr.repo,
                pr_number=pr.number,
                head_sha=pr.head_sha,
                actor_id=pr.author.user_id,
                dedupe_key=f"pr_opened:{pr.repo}:{pr.number}:{pr.head_sha}",
            ),
            "accepted",
        )

    def classify_comment(self, comment: Comment) -> Decision:
        """Accept an agent mention written by an allowlisted commenter.

        Draft state is deliberately not checked here. ``draft`` exists to
        stop the agent auto-reviewing work in progress nobody asked about;
        an allowlisted human typing ``@claude`` on a draft *is* the ask, and
        refusing it would make the handle unreliable exactly when a
        contributor wants early feedback.
        """
        decision = self._decide_comment(comment)
        self._log(
            decision, repo=comment.repo, pr_number=comment.pr_number, kind="mention"
        )
        return decision

    def _decide_comment(self, comment: Comment) -> Decision:
        if self._is_self(comment.author):
            return Decision(None, "self_commenter")
        if comment.author.is_bot:
            return Decision(None, "bot_commenter")
        if not has_mention(comment.body, self.handle):
            return Decision(None, "no_mention")
        if not self.allowlist.allows(comment.author):
            return Decision(None, "commenter_not_allowlisted")
        return Decision(
            Trigger(
                kind=TriggerKind.MENTION,
                repo=comment.repo,
                pr_number=comment.pr_number,
                head_sha=comment.head_sha,
                actor_id=comment.author.user_id,
                dedupe_key=f"mention:{comment.repo}:{comment.pr_number}:{comment.comment_id}",
            ),
            "accepted",
        )

    @staticmethod
    def _log(decision: Decision, *, repo: str, pr_number: int, kind: str) -> None:
        """Surface every decision, not just accepted ones -- this is the only
        observability the daemon has into "why wasn't this reviewed".

        Rejections that an operator would ask about are logged at ``INFO``,
        where the default level shows them. Only the two that fire on every
        poll are held back to ``DEBUG``.
        """
        noisy = decision.reason in NOISY_REASONS
        level = logging.DEBUG if noisy and not decision.accepted else logging.INFO
        logger.log(
            level,
            "trigger decision kind=%s repo=%s pr=%s reason=%s",
            kind,
            repo,
            pr_number,
            decision.reason,
        )
