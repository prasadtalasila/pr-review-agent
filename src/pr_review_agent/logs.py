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
alone, and the three noisy third-party loggers follow it only downwards --
see :func:`_third_party_level`. ``LOG_LEVEL=DEBUG`` is the first thing
reached for in an incident, so per **CLAUDE.md** §5 that bound has a test.

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

#: Third-party loggers the resolved level is allowed to quieten but not to
#: make louder than ``WARNING``. See :func:`_third_party_level`.
THIRD_PARTY_LOGGERS = ("httpx", "httpcore", "asyncio")

#: The loudest these three may get, whatever the operator asks for.
THIRD_PARTY_FLOOR = logging.WARNING

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


def _third_party_level(level: str) -> int:
    """What ``httpx``, ``httpcore`` and ``asyncio`` are set to under ``level``.

    They follow the operator's level *downwards* and stop at ``WARNING``
    going up. Asking for ``ERROR`` really does silence their warnings --
    which pinning them would not have done -- while the two levels below the
    floor are refused for different reasons:

    ``DEBUG`` is a security floor. ``httpx`` and ``httpcore`` log request
    headers at ``DEBUG``, which means ``GITHUB_TOKEN`` in the operator's
    journal, and ``--log-level DEBUG`` is the first thing anyone reaches for
    during an incident -- so the one level that leaks is the one most likely
    to be asked for.

    ``INFO`` is a noise floor. ``httpx`` logs a line per request there, and
    the poller makes several every cycle for as long as the daemon runs;
    that would bury the six events ``docs/LOGGING.md`` says ``INFO`` is for.
    The per-request view is what ``poller`` itself logs at ``DEBUG``.
    """
    # `getattr`, not `logging.getLevelNamesMapping()`: that is 3.11+ and the
    # supported range starts at 3.10. `level` is already one of `LEVELS`, so
    # the attribute exists.
    return max(getattr(logging, level), THIRD_PARTY_FLOOR)


def configure(level: str) -> None:
    """Send this package's records to stderr at ``level``.

    The root logger is left at ``WARNING`` rather than at ``level``: it is
    the parent of every third-party logger in the process, including ones
    not named here, and raising it is what would put ``httpx``'s request
    headers into the log.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    logging.getLogger(PACKAGE_LOGGER).setLevel(level)
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(_third_party_level(level))
