"""How verbose the daemon is, and who gets to say so.

One global level, resolved from three layers -- ``--log-level``, then
``PR_REVIEW_AGENT_LOG_LEVEL``, then ``logging.level`` in ``config.yaml`` --
in that order of precedence, per clig.dev's configuration order. The
environment layer is the one deployment uses: ``GITHUB_TOKEN`` already
arrives that way, so a systemd unit already has an ``Environment=`` block and
the level lands beside it without touching ``ExecStart=``.

Two things this deliberately does not do, both recorded in
:doc:`LOGGING.md <../../docs/LOGGING>`:

**It does not configure the root logger.** ``basicConfig`` does, which is why
a naive ``--log-level DEBUG`` today would switch on ``httpx`` and
``httpcore`` -- and those print request headers, meaning ``GITHUB_TOKEN``,
into the operator's journal. The level moves the ``pr_review_agent`` logger
alone, and the three noisy third-party loggers are pinned at ``WARNING``
whatever the operator asks for. ``LOG_LEVEL=DEBUG`` is the first thing
reached for in an incident, so per **CLAUDE.md** §5 that pinning has a test.

**It offers no per-logger map.** Selection lives in the levels assigned at
the call sites, so a global ``INFO`` is already the operator's view; the
per-component slice is taken at query time from the logger name instead.
"""

from __future__ import annotations

import logging
import os
import sys

#: The levels an operator may name, loudest last. Spelled out rather than
#: taken from ``logging.getLevelName``, which also answers to ``WARN``,
#: ``FATAL`` and any integer -- aliases nothing documents and no unit sets.
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: What the daemon logs at when no layer says otherwise.
DEFAULT_LEVEL = "INFO"

#: The environment layer's name. Prefixed, unlike ``GITHUB_TOKEN``, because
#: a bare ``LOG_LEVEL`` in a unit's ``Environment=`` block is inherited by
#: every subprocess the daemon spawns -- ``claude`` and ``git`` among them.
LEVEL_ENV_VAR = "PR_REVIEW_AGENT_LOG_LEVEL"

#: Pinned at ``WARNING`` regardless of the resolved level. The first two
#: log request headers at ``DEBUG``; ``asyncio`` logs per-task chatter that
#: says nothing about a review.
QUIET_LOGGERS = ("httpx", "httpcore", "asyncio")

#: The logger the level is applied to: this package, and nothing above it.
PACKAGE_LOGGER = "pr_review_agent"

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class LevelError(ValueError):
    """Raised when a layer names a level that does not exist."""


def parse_level(value: str, *, source: str) -> str:
    """The canonical spelling of ``value``, or :class:`LevelError`.

    ``source`` names the layer, because the three of them fail in different
    places and an operator with a typo needs to know which one to edit.
    """
    level = value.strip().upper()
    if level not in LEVELS:
        raise LevelError(f"{source} must be one of {', '.join(LEVELS)}, got {value!r}")
    return level


def resolve_level(flag: str | None, configured: str) -> str:
    """The level in force: flag, then environment, then ``configured``.

    ``configured`` has already been validated by the config loader, and the
    flag by Click's ``Choice``; only the environment layer is unchecked when
    it arrives here, which is why it is the only one parsed.
    """
    if flag is not None:
        return parse_level(flag, source="--log-level")
    from_env = os.environ.get(LEVEL_ENV_VAR)
    if from_env:
        return parse_level(from_env, source=LEVEL_ENV_VAR)
    return configured


def configure(level: str) -> None:
    """Send this package's records to stderr at ``level``.

    The root logger is left at ``WARNING`` rather than at ``level``: it is
    the parent of every third-party logger in the process, and raising it is
    what would put ``httpx``'s request headers into the log.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    logging.getLogger(PACKAGE_LOGGER).setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
