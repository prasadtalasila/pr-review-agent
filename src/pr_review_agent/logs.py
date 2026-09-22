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
alone, and three third-party loggers follow it only as far down as each one
stays quiet -- see :data:`THIRD_PARTY_FLOORS`. ``LOG_LEVEL=DEBUG`` is the
first thing reached for in an incident, so per **CLAUDE.md** §5 the whole
table has a test rather than just attention.

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
#: make louder than their own floor, which is **the lowest level at which
#: that library is quiet**. One rule, three different answers, because the
#: three get loud at three different levels. See :func:`_third_party_level`.
#:
#: The numbers are measured against the pinned ``httpx`` and ``httpcore``,
#: one request each:
#:
#: ``httpx``
#:     Says nothing at DEBUG and one line per request at INFO
#:     (``HTTP Request: GET https://... "HTTP/1.1 200 OK"``). The poller
#:     makes several every cycle for as long as the daemon runs, so INFO is
#:     where it would bury the six events that level exists to show.
#: ``httpcore``
#:     Says nothing at INFO and fourteen records per request at DEBUG,
#:     including the response header list verbatim. It is also the half of
#:     the pair the credential actually travels through, and precisely what
#:     a transport prints about itself is a library-version detail rather
#:     than a contract -- so it is held above DEBUG whatever is installed.
#: ``asyncio``
#:     One record for the whole process (``Using selector: EpollSelector``),
#:     no credential anywhere near it, and nothing per request. There is
#:     nothing to hold back, so it simply follows the operator's level.
THIRD_PARTY_FLOORS = {
    "httpx": logging.WARNING,
    "httpcore": logging.INFO,
    "asyncio": logging.DEBUG,
}

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


def _third_party_level(name: str, level: str) -> int:
    """What third-party logger ``name`` is set to under ``level``.

    Each one follows the operator's level *downwards* without limit, and
    upwards only as far as its own floor -- so ``--log-level ERROR`` really
    does silence their warnings too, which one shared pin would not have
    done, and ``--log-level DEBUG`` still does not turn the transport pair
    on.

    The asymmetry is the point. Quietening a library can only cost an
    operator information they explicitly asked not to have; making one
    louder than its floor costs them the review log, and in ``httpcore``'s
    case prints transport internals nobody asked for.
    """
    # `getattr`, not `logging.getLevelNamesMapping()`: that is 3.11+ and the
    # supported range starts at 3.10. `level` is already one of `LEVELS`, so
    # the attribute exists.
    return max(getattr(logging, level), THIRD_PARTY_FLOORS[name])


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
    for name in THIRD_PARTY_FLOORS:
        logging.getLogger(name).setLevel(_third_party_level(name, level))
