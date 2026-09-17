"""Data types shared by the trigger pipeline.

These mirror only the fields the classifier needs from the GitHub REST
payloads, so the trigger logic can be tested without any network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .._compat import StrEnum


@dataclass(frozen=True)
class Actor:
    """A GitHub account responsible for an event."""

    user_id: int
    login: str
    is_bot: bool = False

    @classmethod
    def from_api(cls, payload: dict) -> Actor:
        """Build an Actor from a GitHub REST ``user`` object."""
        login = payload["login"]
        return cls(
            user_id=int(payload["id"]),
            login=login,
            is_bot=payload.get("type") == "Bot" or login.endswith("[bot]"),
        )


@dataclass(frozen=True)
class PullRequest:
    """An open pull request as reported by the poller."""

    repo: str
    number: int
    head_sha: str
    author: Actor
    created_at: datetime
    is_draft: bool = False


@dataclass(frozen=True)
class Comment:
    """A PR conversation comment or an inline diff comment."""

    repo: str
    pr_number: int
    comment_id: int
    author: Actor
    body: str
    head_sha: str


class TriggerKind(StrEnum):
    """The only two events that may start a review."""

    PR_OPENED = "pr_opened"
    MENTION = "mention"


@dataclass(frozen=True)
class Trigger:
    """An accepted request to review ``head_sha`` of a pull request."""

    kind: TriggerKind
    repo: str
    pr_number: int
    head_sha: str
    actor_id: int
    dedupe_key: str


@dataclass(frozen=True)
class Decision:
    """The classifier verdict, carrying why an event was rejected."""

    trigger: Trigger | None
    reason: str

    @property
    def accepted(self) -> bool:
        """Whether the event produced a trigger."""
        return self.trigger is not None
