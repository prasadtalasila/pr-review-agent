"""Load and validate ``config.yaml``.

Only the sections backed by implemented components are accepted. Unknown
keys are rejected rather than ignored: a typo in a safety setting must fail
at startup, not silently fall back to a default that spends tokens.

One module per group of sections, because the reasoning is per section and
there is a lot of it: :mod:`.budget` holds everything that decides what may
be spent, :mod:`.github_triggers` who is listened to, :mod:`.engine` which
tool runs, :mod:`.runtime` where the daemon writes and how loudly. What is
left here is the document itself -- which sections exist, which are
required, and how a file becomes a :class:`Config`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ..triggers.classifier import Classifier
from ._sections import ConfigError, _keys, _section
from .budget import (
    DEFAULT_MAX_CHANGED_FILES,
    DEFAULT_MAX_CHANGED_LINES,
    DEFAULT_REVIEWER_SHARE_PCT,
    SHARED_FIELDS,
    BudgetConfig,
)
from .engine import DEFAULT_ENGINE_BINARY, EngineConfig
from .excluded_paths import DEFAULT_EXCLUDED_PATHS
from .github_triggers import GitHubConfig, TriggerConfig
from .runtime import (
    DEFAULT_CACHE_DIR,
    DEFAULT_GIT_BINARY,
    DEFAULT_MAX_PUBLISH_ATTEMPTS,
    DEFAULT_STORE_PATH,
    DEFAULT_WORKERS,
    MAX_WORKERS,
    LoggingConfig,
    PublishConfig,
    StoreConfig,
    WorkerConfig,
    WorkspaceConfig,
)

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_ENGINE_BINARY",
    "DEFAULT_EXCLUDED_PATHS",
    "DEFAULT_GIT_BINARY",
    "DEFAULT_MAX_CHANGED_FILES",
    "DEFAULT_MAX_CHANGED_LINES",
    "DEFAULT_MAX_PUBLISH_ATTEMPTS",
    "DEFAULT_REVIEWER_SHARE_PCT",
    "DEFAULT_STORE_PATH",
    "DEFAULT_WORKERS",
    "MAX_WORKERS",
    "SHARED_FIELDS",
    "BudgetConfig",
    "Config",
    "ConfigError",
    "EngineConfig",
    "GitHubConfig",
    "LoggingConfig",
    "PublishConfig",
    "StoreConfig",
    "TriggerConfig",
    "WorkerConfig",
    "WorkspaceConfig",
]


@dataclass(frozen=True)
class Config:
    """The whole configuration file."""

    github: GitHubConfig
    triggers: TriggerConfig
    budget: BudgetConfig
    store: StoreConfig
    workspace: WorkspaceConfig
    worker: WorkerConfig
    publish: PublishConfig
    #: Optional, and its default cannot spend anything: a level decides how
    #: much the daemon says, never what it does.
    logging: LoggingConfig
    #: Required, because the worker now wires it. While nothing drained the
    #: queue an absent section could not spend and so could be absent; a
    #: daemon that claims work and has no engine to run would instead fail
    #: every review after reserving allowance for it.
    engine: EngineConfig

    @classmethod
    def from_mapping(cls, data: Any) -> Config:
        """Validate an already-parsed YAML document."""
        if not isinstance(data, dict):
            raise ConfigError("configuration root must be a mapping")
        unknown = sorted(set(data) - _keys(cls))
        if unknown:
            raise ConfigError(f"unknown top-level sections: {unknown}")
        return cls(
            github=GitHubConfig.parse(_section(data, "github", GitHubConfig)),
            triggers=TriggerConfig.parse(_section(data, "triggers", TriggerConfig)),
            # Required, token counts and all, even when `enabled` is false:
            # every one of them is a guess the operator has to make, and a
            # guess that ships as a default is a spending ceiling nobody
            # chose. Requiring them unconditionally also means flipping the
            # kill switch back on over SIGHUP cannot fail on a key that was
            # never supplied.
            budget=BudgetConfig.parse(_section(data, "budget", BudgetConfig)),
            # The only optional section: its default cannot spend anything,
            # because cold-start seeding bounds a fresh database to the
            # moment the daemon started. Unknown keys inside it are still
            # rejected, so a typo in a path is not silently ignored.
            store=StoreConfig.parse(
                _section(data, "store", StoreConfig) if "store" in data else {}
            ),
            # Optional for the same reason as `store`, though not the same
            # argument: it holds a path and nothing else, because the cap
            # that could spend lives in `budget`.
            workspace=WorkspaceConfig.parse(
                _section(data, "workspace", WorkspaceConfig)
                if "workspace" in data
                else {}
            ),
            # Optional, and its default is the safe one: a single worker
            # holds a single reservation.
            worker=WorkerConfig.parse(
                _section(data, "worker", WorkerConfig) if "worker" in data else {}
            ),
            # Optional, and its default is to post. A dry run spends exactly
            # what a real review spends, so defaulting to one would burn the
            # allowance and show nobody the result.
            publish=PublishConfig.parse(
                _section(data, "publish", PublishConfig) if "publish" in data else {}
            ),
            # Optional, like `store` and `workspace`, and for the same
            # reason: its default is a level, which cannot spend anything.
            logging=LoggingConfig.parse(
                _section(data, "logging", LoggingConfig) if "logging" in data else {}
            ),
            # Required: there is no model an operator could be assumed to
            # have chosen, and every key here decides a cost.
            engine=EngineConfig.parse(_section(data, "engine", EngineConfig)),
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

    def classifier(
        self,
        since: datetime,
        open_pull_requests: frozenset[int] | None = None,
        posted_comment_ids: frozenset[int] = frozenset(),
    ) -> Classifier:
        """Build the classifier this configuration describes."""
        return Classifier(
            allowlist=self.triggers.allowlist,
            since=since,
            handle=self.triggers.handle,
            open_pull_requests=open_pull_requests,
            posted_comment_ids=posted_comment_ids,
        )
