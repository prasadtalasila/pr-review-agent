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


#: Resolved against the working directory the daemon is started in, which is
#: why the daemon logs the absolute path it settled on.
DEFAULT_STORE_PATH = "state.db"

#: Where the bare mirrors and the per-run checkouts live. Resolved the same
#: way, and logged absolute for the same reason.
DEFAULT_CACHE_DIR = ".cache/repos"


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


#: BUDGET.md's human-headroom default: the agent may use this percentage of
#: each plan window, never the whole allowance.
DEFAULT_REVIEWER_SHARE_PCT = 40

#: BUDGET.md layer 2's diff-size caps. Unlike the plan token counts these do
#: have defaults: a plan's allowance is unpublished, so any default would be
#: a fabricated ceiling, whereas a diff-size cap is an ordinary engineering
#: choice. `tests/test_config.py` pins both by value.
DEFAULT_MAX_CHANGED_FILES = 100
DEFAULT_MAX_CHANGED_LINES = 5000


def _tokens(data: dict, key: str) -> int:
    """Read a required positive token count from the ``budget`` section.

    ``bool`` is excluded explicitly because it is a subclass of ``int``, so
    ``budget.weekly_tokens: true`` would otherwise validate as ``1`` -- a
    spending ceiling of one token, arrived at silently.
    """
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"budget.{key} must be a positive number of tokens")
    return value


def _cap(data: dict, key: str, default: int) -> int:
    """Read an optional positive diff-size cap from the ``budget`` section."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"budget.{key} must be a positive integer")
    return value


@dataclass(frozen=True)
class BudgetConfig:
    """The spending rails: what the plan is assumed to allow, and our share.

    ``session_tokens`` and ``weekly_tokens`` are the operator's estimate of
    the *plan's* limits, because a subscription publishes no quota. The
    agent's own ceiling is that times ``reviewer_share_pct``, which is what
    keeps a runaway agent from locking a maintainer out of interactive Claude
    Code.

    ``enabled: false`` **stops reviewing**; it does not stop checking. The
    two readings of a "kill switch" differ by catastrophe -- one is an
    emergency brake, the other is unbounded spend -- so the governor refuses
    every claim while it is false.
    """

    # A configuration section is a flat list of keys. Splitting it to satisfy
    # the attribute count would scatter the spending rails across two types,
    # which is exactly what keeping them in one section is for.
    # pylint: disable=too-many-instance-attributes

    session_tokens: int
    weekly_tokens: int
    max_run_tokens: int
    enabled: bool = True
    reviewer_share_pct: int = DEFAULT_REVIEWER_SHARE_PCT
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES
    #: Optional, and off by default: on a one-person allowlist any cap below
    #: 100 % would block the only account that can trigger anything.
    per_contributor_pct: int | None = None

    @property
    def session_limit(self) -> int:
        """The agent's share of the plan's rolling five-hour window."""
        return self._share(self.session_tokens)

    @property
    def weekly_limit(self) -> int:
        """The agent's share of the plan's rolling weekly window."""
        return self._share(self.weekly_tokens)

    @property
    def daily_limit(self) -> int:
        """A flat seventh of the weekly limit, over a trailing 24 hours.

        A rolling weekly window never resets, so the ``weekly_remaining /
        days_remaining`` pacing BUDGET.md describes has no divisor to use.
        A seventh needs no week anchor and is stricter: an agent idle since
        Monday cannot burn four days' allowance on Friday.
        """
        return self._share(self.weekly_tokens) // 7

    @property
    def per_contributor_limit(self) -> int | None:
        """One contributor's share of the agent's weekly allowance.

        ``None`` when the key is unset, which is how "no per-contributor
        cap" is spelled: the window is simply not measured.
        """
        if self.per_contributor_pct is None:
            return None
        return self.weekly_limit * self.per_contributor_pct // 100

    def _share(self, plan_tokens: int) -> int:
        return plan_tokens * self.reviewer_share_pct // 100

    @classmethod
    def parse(cls, data: dict) -> BudgetConfig:
        """Validate the ``budget`` section."""
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError("budget.enabled must be true or false")
        share = data.get("reviewer_share_pct", DEFAULT_REVIEWER_SHARE_PCT)
        if isinstance(share, bool) or not isinstance(share, int):
            raise ConfigError("budget.reviewer_share_pct must be a whole percentage")
        if not 1 <= share <= 100:
            # Zero would make every limit zero and utilisation an undefined
            # 0/0; an operator who wants the agent stopped has `enabled`.
            raise ConfigError("budget.reviewer_share_pct must be between 1 and 100")
        per_contributor = data.get("per_contributor_pct")
        if per_contributor is not None and (
            isinstance(per_contributor, bool)
            or not isinstance(per_contributor, int)
            or not 1 <= per_contributor <= 100
        ):
            raise ConfigError(
                "budget.per_contributor_pct must be a whole percentage "
                "between 1 and 100, or absent for no cap"
            )
        config = cls(
            session_tokens=_tokens(data, "session_tokens"),
            weekly_tokens=_tokens(data, "weekly_tokens"),
            max_run_tokens=_tokens(data, "max_run_tokens"),
            enabled=enabled,
            reviewer_share_pct=share,
            max_changed_files=_cap(
                data, "max_changed_files", DEFAULT_MAX_CHANGED_FILES
            ),
            max_changed_lines=_cap(
                data, "max_changed_lines", DEFAULT_MAX_CHANGED_LINES
            ),
            per_contributor_pct=per_contributor,
        )
        if (
            config.per_contributor_limit is not None
            and config.per_contributor_limit < config.max_run_tokens
        ):
            # The same trap as the daily check below, one window further: a
            # cap this low admits nobody, including the only contributor on
            # a one-person allowlist.
            raise ConfigError(
                f"budget.max_run_tokens ({config.max_run_tokens}) exceeds one "
                f"contributor's allowance ({config.per_contributor_limit}); "
                "no run could ever be admitted"
            )
        if config.daily_limit < config.max_run_tokens:
            # The daily window is the tightest of the three, so a run that
            # cannot fit inside it can never be admitted at all -- a config
            # that reviews nothing, arrived at by arithmetic nobody did.
            raise ConfigError(
                f"budget.max_run_tokens ({config.max_run_tokens}) exceeds the daily "
                f"allowance ({config.daily_limit}); no run could ever be admitted"
            )
        return config


@dataclass(frozen=True)
class StoreConfig:
    """Where the SQLite state file lives."""

    path: str = DEFAULT_STORE_PATH

    @classmethod
    def parse(cls, data: dict) -> StoreConfig:
        """Validate the ``store`` section."""
        path = data.get("path", DEFAULT_STORE_PATH)
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"store.path must be a non-empty path, got {path!r}")
        return cls(path=path)


@dataclass(frozen=True)
class WorkspaceConfig:
    """Where a pull request is checked out.

    A path and nothing else. The cap that bounds what a checkout may cost
    lives in ``budget`` with every other spending rail.
    """

    cache_dir: str = DEFAULT_CACHE_DIR

    @classmethod
    def parse(cls, data: dict) -> WorkspaceConfig:
        """Validate the ``workspace`` section."""
        cache_dir = data.get("cache_dir", DEFAULT_CACHE_DIR)
        if not isinstance(cache_dir, str) or not cache_dir.strip():
            raise ConfigError(
                f"workspace.cache_dir must be a non-empty path, got {cache_dir!r}"
            )
        return cls(cache_dir=cache_dir)


@dataclass(frozen=True)
class Config:
    """The whole configuration file."""

    github: GitHubConfig
    triggers: TriggerConfig
    budget: BudgetConfig
    store: StoreConfig
    workspace: WorkspaceConfig

    @classmethod
    def from_mapping(cls, data: Any) -> Config:
        """Validate an already-parsed YAML document."""
        if not isinstance(data, dict):
            raise ConfigError("configuration root must be a mapping")
        unknown = sorted(
            set(data) - {"github", "triggers", "budget", "store", "workspace"}
        )
        if unknown:
            raise ConfigError(f"unknown top-level sections: {unknown}")
        return cls(
            github=GitHubConfig.parse(
                _section(data, "github", {"repo", "agent_user_id"})
            ),
            triggers=TriggerConfig.parse(
                _section(data, "triggers", {"allowlist", "handle"})
            ),
            # Required, token counts and all, even when `enabled` is false:
            # every one of them is a guess the operator has to make, and a
            # guess that ships as a default is a spending ceiling nobody
            # chose. Requiring them unconditionally also means flipping the
            # kill switch back on over SIGHUP cannot fail on a key that was
            # never supplied.
            budget=BudgetConfig.parse(
                _section(
                    data,
                    "budget",
                    {
                        "enabled",
                        "session_tokens",
                        "weekly_tokens",
                        "max_run_tokens",
                        "reviewer_share_pct",
                        "per_contributor_pct",
                        "max_changed_files",
                        "max_changed_lines",
                    },
                )
            ),
            # The only optional section: its default cannot spend anything,
            # because cold-start seeding bounds a fresh database to the
            # moment the daemon started. Unknown keys inside it are still
            # rejected, so a typo in a path is not silently ignored.
            store=StoreConfig.parse(
                _section(data, "store", {"path"}) if "store" in data else {}
            ),
            # Optional for the same reason as `store`, though not the same
            # argument: it holds a path and nothing else, because the cap
            # that could spend lives in `budget`.
            workspace=WorkspaceConfig.parse(
                _section(data, "workspace", {"cache_dir"})
                if "workspace" in data
                else {}
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
