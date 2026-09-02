"""Decide whether a polled event may start a review.

Exactly two events qualify: a freshly opened pull request by an allowlisted
author, and a comment mentioning the agent from an allowlisted commenter.
Pushes to an existing pull request are deliberately ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .allowlist import Allowlist
from .mention import has_mention
from .models import Actor, Comment, Decision, PullRequest, Trigger, TriggerKind


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

    def _is_self(self, actor: Actor) -> bool:
        return self.agent_user_id is not None and actor.user_id == self.agent_user_id

    def classify_pull_request(self, pr: PullRequest) -> Decision:
        """Accept a freshly opened pull request from an allowlisted author."""
        if pr.author.is_bot or self._is_self(pr.author):
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
        """Accept an agent mention written by an allowlisted commenter."""
        if comment.author.is_bot or self._is_self(comment.author):
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
