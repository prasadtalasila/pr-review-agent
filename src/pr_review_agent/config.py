"""Load and validate ``config.yaml``.

Only the sections backed by implemented components are accepted. Unknown
keys are rejected rather than ignored: a typo in a safety setting must fail
at startup, not silently fall back to a default that spends tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .triggers.allowlist import Allowlist, AllowlistConfigError
from .triggers.classifier import Classifier


class ConfigError(ValueError):
    """Raised when the configuration file is unusable."""


def _section(data: dict, name: str, allowed: set[str]) -> dict:
    """Return section ``name``, rejecting unknown keys inside it."""
    value = data.get(name)
    if value is None:
        raise ConfigError(f"missing required section: {name!r}")
    if not isinstance(value, dict):
        raise ConfigError(f"section {name!r} must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown keys in {name!r}: {unknown}")
    return value


@dataclass(frozen=True)
class GitHubConfig:
    """The repository to poll and the agent's own identity."""

    repo: str
    agent_user_id: int | None = None

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
        agent_id = data.get("agent_user_id")
        if agent_id is not None and not isinstance(agent_id, int):
            raise ConfigError("github.agent_user_id must be a numeric user id")
        return cls(repo=repo, agent_user_id=agent_id)


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


@dataclass(frozen=True)
class Config:
    """The whole configuration file."""

    github: GitHubConfig
    triggers: TriggerConfig

    @classmethod
    def from_mapping(cls, data: Any) -> Config:
        """Validate an already-parsed YAML document."""
        if not isinstance(data, dict):
            raise ConfigError("configuration root must be a mapping")
        unknown = sorted(set(data) - {"github", "triggers"})
        if unknown:
            raise ConfigError(f"unknown top-level sections: {unknown}")
        return cls(
            github=GitHubConfig.parse(
                _section(data, "github", {"repo", "agent_user_id"})
            ),
            triggers=TriggerConfig.parse(
                _section(data, "triggers", {"allowlist", "handle"})
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> Config:
        """Read and validate a YAML configuration file."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {str(path)!r}: {exc}") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {str(path)!r}: {exc}") from exc
        return cls.from_mapping(data)

    def classifier(self, since: datetime) -> Classifier:
        """Build the classifier this configuration describes."""
        return Classifier(
            allowlist=self.triggers.allowlist,
            since=since,
            agent_user_id=self.github.agent_user_id,
            handle=self.triggers.handle,
        )
