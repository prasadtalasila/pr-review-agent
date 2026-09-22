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
#: already-seen item, ``no_mention`` once per comment, ``pr_not_open`` once
#: per comment on every pull request the repository has ever closed. They
#: stay at ``DEBUG`` so the operator-relevant rejections are readable at
#: ``INFO``.


@dataclass(frozen=True)
class Classifier:
    """Applies eligibility rules to polled pull requests and comments.

    ``since`` is the cold-start watermark. The poller sees *open* pull
    requests rather than ``opened`` webhook events, so without it the first
    poll would treat every already-open pull request as fresh and review the
    whole backlog at once.

    ``open_pull_requests`` is the set of numbers the ``/pulls?state=open``
    leg of the same sweep reported. The two comment endpoints are repo-wide
    and carry no state filter of their own, so without it every comment on
    every pull request the repository has ever closed is classified on every
    poll. ``None`` means no sweep has reported one yet and the filter is
    off: failing open costs a few ``DEBUG`` lines, whereas failing closed
    would silently drop every mention.
    """

    allowlist: Allowlist
    since: datetime
    agent_user_id: int | None = None
    handle: str = "claude"
    open_pull_requests: frozenset[int] | None = None

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
        if pr.created_at <= self.since:
            return Decision(None, "not_fresh")
        if self._is_self(pr.author):
            return Decision(None, "self_author")
        if pr.author.is_bot:
            return Decision(None, "bot_author")
        if pr.is_draft:
            return Decision(None, "draft")
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

        Freshness *is* checked, against the same ``since`` watermark the
        pull-request path uses. Without it, a fresh database replays every
        historical mention as a new request. An edit bumps ``updated_at``, so
        editing ``@claude`` into an old comment does summon a review -- which
        is the correct reading of an allowlisted maintainer's intent.

        A mention on a pull request that closed between two cycles is
        dropped as ``pr_not_open``. That is a behaviour change and not only
        a quieter log, and it is the intended reading: the agent has nothing
        useful to say about a closed pull request.
        """
        decision = self._decide_comment(comment)
        self._log(
            decision, repo=comment.repo, pr_number=comment.pr_number, kind="mention"
        )
        return decision

    def _decide_comment(self, comment: Comment) -> Decision:
        if comment.updated_at <= self.since:
            return Decision(None, "not_fresh")
        if (
            self.open_pull_requests is not None
            and comment.pr_number not in self.open_pull_requests
        ):
            return Decision(None, "pr_not_open")
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
                comment_id=comment.comment_id,
                comment_source=comment.source,
            ),
            "accepted",
        )

    @staticmethod
    def _log(decision: Decision, *, repo: str, pr_number: int, kind: str) -> None:
        """Surface every decision, not just accepted ones -- this is the only
        observability the daemon has into "why wasn't this reviewed".

        Every decision at ``DEBUG``, accepted ones included. The per-reason
        split this used to make -- operator-relevant rejections at ``INFO``,
        the three firehose reasons at ``DEBUG`` -- is gone: a decision fires
        for every pull request and every comment on every poll, so the whole
        record belongs on the level an operator turns *on* to ask "why
        wasn't this reviewed", not on the one they read by default. What a
        review actually did is events 3 to 6, and those are louder.
        """
        logger.debug(
            "trigger decision kind=%s repo=%s pr=%s reason=%s",
            kind,
            repo,
            pr_number,
            decision.reason,
        )
