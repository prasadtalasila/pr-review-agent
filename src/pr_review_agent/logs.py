"""How verbose the daemon is, how a record is shaped, and who gets to say so.

Two scalars and no destinations. One global level and one format, each
resolved from three layers -- the flag, then the environment, then
``config.yaml`` -- in that order of precedence, per clig.dev's configuration
order. The environment layer is the one deployment uses: ``GITHUB_TOKEN``
already arrives that way, so a systemd unit already has an ``Environment=``
block and both settings land beside it without touching ``ExecStart=``.

The stream itself is always stderr. The daemon opens no files and holds no
list of sinks; duplicating the stream is systemd's job, or rsyslog's. What
is decided here is only the *shape* of a record and whether journald is told
its priority.

Three things this deliberately does not do, all recorded in
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
per-component slice is taken at query time from the ``logger`` field of the
JSON record instead.

**It offers no ``level_prefix`` switch.** Whether records carry journald's
``<N>`` priority prefix is detected, not configured: a boolean would offer
four states, two of them broken, and the broken one puts ``<6>`` in front of
every line of a JSON file, silently. See :func:`_on_journal`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import IO

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

#: The shapes a record may take. ``auto`` is the answer to "who is reading
#: this": text when stderr is a terminal, JSON when anything else is.
FORMATS = ("auto", "text", "json")

#: What the daemon formats as when no layer says otherwise.
DEFAULT_FORMAT = "auto"

#: The format's environment layer, prefixed for the same reason as the
#: level's.
FORMAT_ENV_VAR = "PR_REVIEW_AGENT_LOG_FORMAT"

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

#: What a record looks like for a human. Kept whole, including ``asctime``
#: and ``levelname``, which journald duplicates: this is the terminal shape,
#: and under a unit the JSON one is what is in force.
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: The syslog priority journald stores a record at, per Python level. The
#: single digit of the ``<N>`` prefix; it sets the level only, and the
#: facility stays whatever ``SyslogFacility=`` says.
PRIORITIES = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}

#: Attributes every record carries, which are therefore not ``extra=``.
#: Taken from a throwaway record rather than listed, so a new attribute in a
#: future Python does not leak into the JSON. The three added by hand are
#: set after construction: ``message`` and ``asctime`` by the formatters,
#: ``taskName`` by 3.12's asyncio, which the supported range starts below.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


class LevelError(ValueError):
    """Raised when a layer names a level that does not exist."""


class FormatError(ValueError):
    """Raised when a layer names a format that does not exist."""


def parse_level(value: str, *, source: str) -> str:
    """The canonical spelling of ``value``, or :class:`LevelError`.

    ``source`` names the layer, because the three of them fail in different
    places and an operator with a typo needs to know which one to edit.
    """
    level = value.strip().upper()
    if level not in LEVELS:
        raise LevelError(f"{source} must be one of {', '.join(LEVELS)}, got {value!r}")
    return level


def parse_format(value: str, *, source: str) -> str:
    """The canonical spelling of ``value``, or :class:`FormatError`.

    Lowercase where the level is uppercase, because that is how both are
    spelled in ``config.yaml`` and in ``docs/LOGGING.md``.
    """
    fmt = value.strip().lower()
    if fmt not in FORMATS:
        raise FormatError(
            f"{source} must be one of {', '.join(FORMATS)}, got {value!r}"
        )
    return fmt


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


def resolve_format(flag: str | None, configured: str) -> str:
    """The format in force: flag, then environment, then ``configured``.

    The same three layers as :func:`resolve_level`, and for the same reason:
    a unit overrides the file from its ``Environment=`` block, and a human
    running the daemon by hand overrides both from the command line.
    """
    if flag is not None:
        return parse_format(flag, source="--log-format")
    from_env = os.environ.get(FORMAT_ENV_VAR)
    if from_env:
        return parse_format(from_env, source=FORMAT_ENV_VAR)
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


def _on_journal(stream: IO[str]) -> bool:
    """Whether ``stream`` is the journal socket systemd handed this process.

    The comparison, and not the mere presence of ``JOURNAL_STREAM``, because
    the environment is inherited: the engine adapter spawns ``claude`` and
    the workspace spawns ``git``, both with a pipe for stderr and both
    carrying the variable. A presence check is wrong in exactly those cases,
    in the direction that corrupts their output.

    The cheaper tests do not work either. ``isatty`` is false for the
    journal *and* for a file, a pipe and a container runtime, and
    ``S_ISSOCK`` is true for any socket.
    """
    spec = os.environ.get("JOURNAL_STREAM")
    if not spec:
        return False
    device, _, inode = spec.partition(":")
    try:
        st = os.fstat(stream.fileno())
    except (OSError, ValueError):
        # A stream with no descriptor -- pytest's capture, a StringIO --
        # cannot be the socket systemd opened.
        return False
    return (str(st.st_dev), str(st.st_ino)) == (device, inode)


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with ``extra=`` fields promoted.

    Modelled on ``dockerd``'s own JSON output: a timestamp, a level, the
    logger that spoke and the message, then whatever the call site attached.
    The promoted fields are what makes the stream queryable -- ``.pr``,
    ``.reason``, ``.remaining`` -- rather than a string something downstream
    has to re-parse.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            # One string, so a traceback stays one journal entry at one
            # priority instead of splitting per line.
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class JournalPriority(logging.Formatter):
    """Prefix a rendered record with the priority journald will strip.

    journald gives every line a service writes the priority of
    ``SyslogLevel=``, which defaults to ``info`` -- so without this a worker
    crash is stored at ``PRIORITY=6`` and ``journalctl -p warning`` returns
    nothing, ever. ``SyslogLevelPrefix=`` defaults to yes, so ``<4>`` sets
    the priority and is removed before the message is stored, which is why
    it does not corrupt the JSON.
    """

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return f"<{PRIORITIES[record.levelno]}>{self._inner.format(record)}"


def _formatter(fmt: str, stream: IO[str]) -> logging.Formatter:
    """The formatter ``fmt`` names, with ``auto`` resolved against ``stream``.

    Two separate decisions, resolved by two different tests and conflated at
    the reader's peril: the shape follows ``isatty`` -- human-readable when
    a human is watching -- and the prefix follows journald detection.
    ``isatty`` is false for a file, a pipe, a container runtime *and* the
    journal, so it cannot drive the prefix.
    """
    if fmt == "auto":
        fmt = "text" if stream.isatty() else "json"
    inner = JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT)
    return JournalPriority(inner) if _on_journal(stream) else inner


def configure(level: str, fmt: str = DEFAULT_FORMAT) -> None:
    """Send this package's records to stderr at ``level``, shaped by ``fmt``.

    The root logger is left at ``WARNING`` rather than at ``level``: it is
    the parent of every third-party logger in the process, including ones
    not named here, and raising it is what would put ``httpx``'s request
    headers into the log.

    Both format decisions are taken here, once, and neither is re-evaluated
    per record.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_formatter(fmt, sys.stderr))

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    logging.getLogger(PACKAGE_LOGGER).setLevel(level)
    for name in THIRD_PARTY_FLOORS:
        logging.getLogger(name).setLevel(_third_party_level(name, level))
