"""Eligibility check against configured contributors.

Membership is decided on the numeric GitHub user id, never the login: a
login can be renamed and the freed name registered by somebody else, so a
login-keyed allowlist silently transfers eligibility to a stranger.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Actor


class AllowlistConfigError(ValueError):
    """Raised when configured entries are not usable as numeric user ids."""


@dataclass(frozen=True)
class Allowlist:
    """The set of accounts whose events may start a review."""

    user_ids: frozenset[int]

    @classmethod
    def from_config(cls, entries: list) -> Allowlist:
        """Build from config, rejecting anything that is not a user id.

        Logins are rejected loudly rather than never matching, so a
        misconfigured allowlist fails at startup instead of silently
        disabling every trigger.
        """
        bad = [entry for entry in entries if not _is_user_id(entry)]
        if bad:
            raise AllowlistConfigError(
                f"allowlist entries must be numeric GitHub user ids, got: {bad!r}"
            )
        return cls(frozenset(int(entry) for entry in entries))

    def allows(self, actor: Actor) -> bool:
        """True when ``actor`` is an eligible account."""
        return actor.user_id in self.user_ids


def _is_user_id(entry: object) -> bool:
    """True when ``entry`` is usable as a numeric GitHub user id.

    ``bool`` is excluded explicitly because it is a subclass of ``int``,
    so a stray ``true`` in YAML would otherwise become user id 1.
    """
    if isinstance(entry, bool):
        return False
    if isinstance(entry, int):
        return True
    return isinstance(entry, str) and entry.strip().lstrip("-").isdigit()
