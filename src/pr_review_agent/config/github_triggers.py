"""Which repository is polled, and whose requests are honoured."""

from __future__ import annotations

from dataclasses import dataclass

from ..triggers.allowlist import Allowlist, AllowlistConfigError
from ._sections import ConfigError


@dataclass(frozen=True)
class GitHubConfig:
    """The repository to poll.

    ``agent_user_id`` used to live here, naming the account the agent posts
    as so the classifier could reject its own events. It is gone: the loop it
    guarded is closed in ``publisher.render``, which neutralises the handle
    in every body it posts, and an identity check also rejected a deployment
    that shares one account between the reviewer and the reviewed. A file
    that still carries the key is refused by name -- ``github`` takes no
    unknown keys -- which is the loud failure the operator needs, since the
    fix is to delete one line.
    """

    repo: str

    @property
    def owner(self) -> str:
        """The ``owner`` half of ``owner/name``."""
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        """The ``name`` half of ``owner/name``."""
        return self.repo.split("/", 1)[1]

    @classmethod
    def parse(cls, data: dict) -> GitHubConfig:
        """Validate the ``github`` section."""
        repo = data.get("repo")
        if not isinstance(repo, str) or repo.count("/") != 1:
            raise ConfigError(f"github.repo must be 'owner/name', got {repo!r}")
        if not all(part.strip() for part in repo.split("/")):
            raise ConfigError(f"github.repo must be 'owner/name', got {repo!r}")
        return cls(repo=repo)


@dataclass(frozen=True)
class TriggerConfig:
    """Who may start a review, and the handle that summons one."""

    allowlist: Allowlist
    handle: str = "claude"

    @classmethod
    def parse(cls, data: dict) -> TriggerConfig:
        """Validate the ``triggers`` section."""
        entries = data.get("allowlist")
        if not isinstance(entries, list):
            raise ConfigError("triggers.allowlist must be a list of user ids")
        handle = data.get("handle", "claude")
        if not isinstance(handle, str) or not handle.strip():
            raise ConfigError("triggers.handle must be a non-empty string")
        try:
            allowlist = Allowlist.from_config(entries)
        except AllowlistConfigError as exc:
            raise ConfigError(f"triggers.allowlist: {exc}") from exc
        return cls(allowlist=allowlist, handle=handle.lstrip("@"))
