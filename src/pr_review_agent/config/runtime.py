"""The sections that say where the daemon runs, not what it may spend.

All five are optional and every default here is safe by the same argument:
a path, a level, a worker count and a publish switch decide how much the
daemon says and where it writes, never how much it costs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logs import (
    DEFAULT_FORMAT,
    DEFAULT_LEVEL,
    FormatError,
    LevelError,
    parse_format,
    parse_level,
)
from ._sections import ConfigError

#: Resolved against the working directory the daemon is started in, which is
#: why the daemon logs the absolute path it settled on.
DEFAULT_STORE_PATH = "state.db"

#: Where the bare mirrors and the per-run checkouts live. Resolved the same
#: way, and logged absolute for the same reason.
DEFAULT_CACHE_DIR = ".cache/repos"

#: The git the checkout runs when the operator does not name one.
DEFAULT_GIT_BINARY = "git"

#: Review concurrency. One to start with: a second worker does not merely
#: review faster, it doubles the allowance held in reservations at any
#: moment, and that is a decision an operator should take deliberately.
DEFAULT_WORKERS = 1

#: The ceiling on that decision. Four concurrent runs against a personal
#: plan's share is already generous; beyond it the reservation floor grows
#: faster than any plausible allowance, and every claim would be refused.
MAX_WORKERS = 4

#: Posts tried for one recorded review before it is given up on. Ten rather
#: than the queue's three: the findings are already paid for, so the cost of
#: trying again is one HTTP request, and the cost of stopping too early is a
#: review nobody ever sees. Ten failed posts is no longer a bad afternoon at
#: GitHub -- it is something an operator has to fix.
DEFAULT_MAX_PUBLISH_ATTEMPTS = 10


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
    """Where a pull request is checked out, and what checks it out.

    The cap that bounds what a checkout may cost lives in ``budget`` with
    every other spending rail.

    ``git`` exists so an operator can name an absolute path. The default is
    the plain name, resolved through the ``PATH`` the checkout passes
    through -- which is the one place a writable directory early on that
    ``PATH`` can defeat every other control in ``gitcmd``.
    """

    cache_dir: str = DEFAULT_CACHE_DIR
    git: str = DEFAULT_GIT_BINARY

    @classmethod
    def parse(cls, data: dict) -> WorkspaceConfig:
        """Validate the ``workspace`` section."""
        cache_dir = data.get("cache_dir", DEFAULT_CACHE_DIR)
        if not isinstance(cache_dir, str) or not cache_dir.strip():
            raise ConfigError(
                f"workspace.cache_dir must be a non-empty path, got {cache_dir!r}"
            )
        git = data.get("git", DEFAULT_GIT_BINARY)
        if not isinstance(git, str) or not git.strip():
            raise ConfigError(f"workspace.git must be a non-empty path, got {git!r}")
        return cls(cache_dir=cache_dir, git=git)


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
    #: Post a review whose commit stopped being the head while it ran,
    #: marked as describing that commit. Defaults true because the tokens
    #: are spent before the head is re-read: discarding the review saves
    #: nothing and shows nobody anything. ``false`` restores the older
    #: behaviour, where such a review is recorded and never posted.
    post_superseded: bool = True
    #: How many times posting one recorded review may be tried before the
    #: run is stamped as failed and stops being offered. Deliberately well
    #: above the queue's ``max_attempts``: that bound measures allowance
    #: drained and a post drains none, so the only thing this protects
    #: against is a post GitHub will never accept -- a locked pull request,
    #: a repository with issues disabled -- being retried forever.
    max_publish_attempts: int = DEFAULT_MAX_PUBLISH_ATTEMPTS

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
        post_superseded = data.get("post_superseded", True)
        if not isinstance(post_superseded, bool):
            raise ConfigError(
                "publish.post_superseded must be true or false, "
                f"got {post_superseded!r}"
            )
        attempts = data.get("max_publish_attempts", DEFAULT_MAX_PUBLISH_ATTEMPTS)
        # ``bool`` is an ``int`` in Python, so ``true`` would pass an
        # ``isinstance`` test and then be a limit of one.
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ConfigError(
                "publish.max_publish_attempts must be a positive integer, "
                f"got {attempts!r}"
            )
        return cls(
            dry_run=dry_run,
            post_superseded=post_superseded,
            max_publish_attempts=attempts,
        )


@dataclass(frozen=True)
class LoggingConfig:
    """How verbose the daemon is, and what shape a record takes.

    The lowest-precedence of the three layers for each: ``--log-level`` and
    ``--log-format`` beat the environment, which beats this. It exists so a
    choice made once survives in the same file as everything else about the
    deployment, and a unit that wants to override it has the environment.

    Two scalars and nothing else. No destinations, and no per-logger map --
    see ``docs/LOGGING.md`` for why both were rejected.
    """

    level: str = DEFAULT_LEVEL
    format: str = DEFAULT_FORMAT

    @classmethod
    def parse(cls, data: dict) -> LoggingConfig:
        """Validate the ``logging`` section."""
        level = data.get("level", DEFAULT_LEVEL)
        if not isinstance(level, str):
            raise ConfigError(f"logging.level must be a level name, got {level!r}")
        fmt = data.get("format", DEFAULT_FORMAT)
        if not isinstance(fmt, str):
            raise ConfigError(f"logging.format must be a format name, got {fmt!r}")
        try:
            return cls(
                level=parse_level(level, source="logging.level"),
                format=parse_format(fmt, source="logging.format"),
            )
        except (LevelError, FormatError) as exc:
            raise ConfigError(str(exc)) from exc


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
