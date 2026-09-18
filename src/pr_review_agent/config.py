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

from .queue import DEFAULT_LEASE
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

#: Review concurrency. One to start with: a second worker does not merely
#: review faster, it doubles the allowance held in reservations at any
#: moment, and that is a decision an operator should take deliberately.
DEFAULT_WORKERS = 1

#: The ceiling on that decision. Four concurrent runs against a personal
#: plan's share is already generous; beyond it the reservation floor grows
#: faster than any plausible allowance, and every claim would be refused.
MAX_WORKERS = 4


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
    """The repository to poll and the agent's own identity.

    Both are required. Without ``agent_user_id`` the agent cannot recognise
    its own comments, so a posted review can re-trigger a review of the same
    pull request -- a loop that spends real tokens and is only visible after
    it has run. A field whose absence costs money is not optional.
    """

    repo: str
    agent_user_id: int

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
        # `bool` is excluded for the same reason the token counts exclude it:
        # it is a subclass of `int`, so `agent_user_id: true` would validate
        # as user 1 -- a real account, and not the agent's.
        if isinstance(agent_id, bool) or not isinstance(agent_id, int):
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

#: Paths excluded from both the size gate and the diff the engine is shown.
#: The four categories BUDGET.md layer 2 names -- lockfiles, vendored trees,
#: generated code, minified bundles -- where reviewing a line is close to
#: worthless while the line still counts against a cap.
#:
#: A default rather than a fixed list, because a repository that genuinely
#: reviews its lockfiles exists; and reloadable on ``SIGHUP``, like every
#: other key in this section, so an operator who finds the agent blind to
#: something can fix it without a restart.
DEFAULT_EXCLUDED_PATHS: tuple[str, ...] = (
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/poetry.lock",
    "**/Cargo.lock",
    "**/Gemfile.lock",
    "**/composer.lock",
    "**/go.sum",
    "**/vendor/**",
    "**/node_modules/**",
    "**/third_party/**",
    "**/*.pb.go",
    "**/*_pb2.py",
    "**/*.generated.*",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
)


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


def _excluded_paths(data: dict) -> tuple[str, ...]:
    """Read the optional list of excluded path patterns.

    A pattern may not begin with ``:``. ``exclusions.py`` builds a
    ``:(exclude,glob)`` prefix in front of each one, and a pattern free to
    open magic of its own -- ``:(attr:...)``, or a bare ``:`` re-anchoring
    the path -- is not something an operator can predict from reading their
    own configuration file. It cannot reach outside the argument it sits in;
    it can make that argument mean something else.
    """
    value = data.get("excluded_paths")
    if value is None:
        return DEFAULT_EXCLUDED_PATHS
    if not isinstance(value, list):
        raise ConfigError("budget.excluded_paths must be a list of path patterns")
    for pattern in value:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigError(
                f"budget.excluded_paths entries must be non-empty patterns, "
                f"got {pattern!r}"
            )
        if pattern.startswith(":"):
            raise ConfigError(
                f"budget.excluded_paths entry {pattern!r} may not begin with ':': "
                "the pathspec magic is supplied by the agent"
            )
    return tuple(value)


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
    excluded_paths: tuple[str, ...] = DEFAULT_EXCLUDED_PATHS
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
            excluded_paths=_excluded_paths(data),
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
class PublishConfig:
    """Whether the publisher actually posts.

    ``dry_run`` runs the whole pipeline -- acknowledgement aside -- and posts
    nothing, which is how an operator watches what the agent *would* say
    before letting it say it. It is the second reloadable key, alongside
    ``budget.enabled``, because both are brakes: a brake that needs a restart
    is not one.

    Unlike the budget token counts this has a default, and the default is
    ``False``. A dry run costs the same tokens as a real one and produces no
    review, so defaulting to it would be a daemon that spends the allowance
    and shows nobody the result.
    """

    dry_run: bool = False

    @classmethod
    def parse(cls, data: dict) -> PublishConfig:
        """Validate the ``publish`` section."""
        dry_run = data.get("dry_run", False)
        # Strictly ``bool``: every non-empty string is truthy in Python, so
        # ``dry_run: "no"`` would read as "post for real" under a cast and as
        # "post nothing" under YAML's own boolean rules. Refusing both is the
        # only answer that cannot surprise an operator.
        if not isinstance(dry_run, bool):
            raise ConfigError(f"publish.dry_run must be true or false, got {dry_run!r}")
        return cls(dry_run=dry_run)


#: The CLI an adapter runs when the operator does not name one.
DEFAULT_ENGINE_BINARY = "claude"


@dataclass(frozen=True)
class EngineConfig:
    """Which coding agent reviews, and the rails around one invocation.

    ``model`` and ``expected_version`` have no defaults for the same reason
    the plan token counts have none: a default model is a cost nobody chose,
    and a default version pin is a claim about output nobody checked.

    ``standards_paths`` are read from the repository under review **at the
    merge base**, never at the pull request head, so opening a pull request
    cannot rewrite the reviewer's instructions.
    """

    model: str
    expected_version: str
    timeout_seconds: float
    binary: str = DEFAULT_ENGINE_BINARY
    standards_paths: tuple[str, ...] = ()

    @classmethod
    def parse(cls, data: dict) -> EngineConfig:
        """Validate the ``engine`` section."""
        return cls(
            model=_text(data, "model"),
            expected_version=_text(data, "expected_version"),
            timeout_seconds=_timeout(data),
            binary=(
                _text(data, "binary") if "binary" in data else DEFAULT_ENGINE_BINARY
            ),
            standards_paths=_standards_paths(data),
        )


def _text(data: dict, key: str) -> str:
    """Read a required non-empty string from the ``engine`` section."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"engine.{key} must be a non-empty string")
    return value


def _timeout(data: dict) -> float:
    """Read the wall clock one review may not outlive.

    Bounded above by the queue lease. ``queue.py`` calls ``DEFAULT_LEASE``
    "comfortably above the per-run wall-clock ceiling the budget governor
    enforces", and its module docstring goes further: a lease carries an
    expiry rather than a heartbeat *because* a run has a ceiling, so a
    renewal "would be machinery for a case that cannot arise". An operator
    who sets an hour makes that case arise -- the lease lapses under a live
    worker, a second worker claims the same pull request and reserves against
    the same windows, and the first worker's ``settle`` returns ``False`` and
    discards a review that was paid for. Until now the invariant was a
    comment.

    Strictly below, not equal: at exactly the lease the two expire together
    and which one wins is a scheduling race.
    """
    value = data.get("timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError("engine.timeout_seconds must be a positive number")
    lease = DEFAULT_LEASE.total_seconds()
    if value >= lease:
        raise ConfigError(
            f"engine.timeout_seconds ({value:g}) must be below the queue lease "
            f"({lease:g}s); a run that outlives its lease loses it mid-review"
        )
    return float(value)


def _standards_paths(data: dict) -> tuple[str, ...]:
    """Read the optional list of standards files."""
    value = data.get("standards_paths", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError("engine.standards_paths must be a list of repository paths")
    return tuple(value)


@dataclass(frozen=True)
class WorkerConfig:
    """How many reviews may run at once.

    A spending control, not a throughput knob: every concurrent run reserves
    ``budget.max_run_tokens`` up front, so ``count`` multiplies the floor
    below which the governor refuses everything. Hence the cap, and hence
    both numbers being pinned by tests -- ``CLAUDE.md`` §5.

    One pull request is never reviewed by two workers whatever this is; the
    queue's per-pull-request lease holds that. Raising it parallelises
    *across* pull requests only.
    """

    count: int = DEFAULT_WORKERS

    @classmethod
    def parse(cls, data: dict) -> WorkerConfig:
        """Validate the ``worker`` section."""
        count = data.get("count", DEFAULT_WORKERS)
        # `bool` is an `int` in Python, and `count: true` is a typo rather
        # than a request for one worker.
        if isinstance(count, bool) or not isinstance(count, int):
            raise ConfigError(f"worker.count must be an integer, got {count!r}")
        if not 1 <= count <= MAX_WORKERS:
            raise ConfigError(
                f"worker.count must be between 1 and {MAX_WORKERS}, got {count}"
            )
        return cls(count=count)


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
        unknown = sorted(
            set(data)
            - {
                "github",
                "triggers",
                "budget",
                "store",
                "workspace",
                "worker",
                "publish",
                "engine",
            }
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
                        "excluded_paths",
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
            # Optional, and its default is the safe one: a single worker
            # holds a single reservation.
            worker=WorkerConfig.parse(
                _section(data, "worker", {"count"}) if "worker" in data else {}
            ),
            # Optional, and its default is to post. A dry run spends exactly
            # what a real review spends, so defaulting to one would burn the
            # allowance and show nobody the result.
            publish=PublishConfig.parse(
                _section(data, "publish", {"dry_run"}) if "publish" in data else {}
            ),
            # Required: there is no model an operator could be assumed to
            # have chosen, and every key here decides a cost.
            engine=EngineConfig.parse(
                _section(
                    data,
                    "engine",
                    {
                        "binary",
                        "model",
                        "timeout_seconds",
                        "standards_paths",
                        "expected_version",
                    },
                )
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
