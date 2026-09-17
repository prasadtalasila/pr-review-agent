"""Data types shared by the trigger pipeline.

These mirror only the fields the classifier needs from the GitHub REST
payloads, so the trigger logic can be tested without any network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .._compat import StrEnum


class PayloadError(ValueError):
    """Raised when a GitHub REST payload cannot be mapped to a model.

    A deleted (ghost) account arrives as ``"user": null``, and an event
    nobody is accountable for cannot be allowlisted. Raising a typed error
    lets the caller skip that one item rather than crash the poll cycle on a
    ``TypeError``.
    """


@dataclass(frozen=True)
class Actor:
    """A GitHub account responsible for an event."""

    user_id: int
    login: str
    is_bot: bool = False

    @classmethod
    def from_api(cls, payload: dict | None) -> Actor:
        """Build an Actor from a GitHub REST ``user`` object."""
        if not isinstance(payload, dict):
            raise PayloadError(f"expected a user object, got {payload!r}")
        try:
            user_id, login = int(payload["id"]), payload["login"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PayloadError(f"unusable user object: {payload!r}") from exc
        return cls(
            user_id=user_id,
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
    """A PR conversation comment or an inline diff comment.

    ``head_sha`` is optional because the issue-comments payload does not
    carry one: a conversation comment is attached to the pull request, not
    to a commit. Resolving it here would cost one extra API call per comment
    on every poll, so it is left unresolved and read at claim time instead --
    which is also the only moment at which it is still correct.

    ``updated_at`` is what the ``comments`` watermark advances on. Both
    comment endpoints are sorted by it and it only ever moves forward, so a
    single high-water mark cannot hide a comment that surfaces later. An edit
    bumps it, which is deliberate: editing ``@claude`` into an existing
    comment is a request. A comment that was already a mention is stopped
    from being reviewed twice by its dedupe key, not by the watermark.
    """

    repo: str
    pr_number: int
    comment_id: int
    author: Actor
    body: str
    updated_at: datetime
    head_sha: str | None = None


class TriggerKind(StrEnum):
    """The only two events that may start a review."""

    PR_OPENED = "pr_opened"
    MENTION = "mention"


@dataclass(frozen=True)
class Trigger:
    """An accepted request to review a pull request.

    ``head_sha`` is ``None`` for a mention whose payload did not name one;
    the worker resolves it when it claims the trigger.
    """

    kind: TriggerKind
    repo: str
    pr_number: int
    head_sha: str | None
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
