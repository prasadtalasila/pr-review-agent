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
        coerced = [(entry, _coerce_user_id(entry)) for entry in entries]
        bad = [entry for entry, user_id in coerced if user_id is None]
        if bad:
            raise AllowlistConfigError(
                f"allowlist entries must be positive GitHub user ids, got: {bad!r}"
            )
        return cls(frozenset(user_id for _, user_id in coerced if user_id is not None))

    def allows(self, actor: Actor) -> bool:
        """True when ``actor`` is an eligible account."""
        return actor.user_id in self.user_ids


def _coerce_user_id(entry: object) -> int | None:
    """``entry`` as a GitHub user id, or ``None`` when it is not one.

    Parsing and validating in one step is what keeps this total: a separate
    shape check followed by ``int()`` let ``"--5"`` pass the check and then
    raise a bare ``ValueError`` out of the loader. ``bool`` is excluded
    explicitly because it is a subclass of ``int``, so a stray ``true`` in
    YAML would otherwise become user id 1. Non-positive values are rejected
    because GitHub user ids are positive.
    """
    if isinstance(entry, bool) or not isinstance(entry, int | str):
        return None
    try:
        value = int(entry)
    except ValueError:
        return None
    return value if value > 0 else None
