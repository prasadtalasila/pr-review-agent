"""The daemon's log level: who sets it, and what it must never turn up.

Two properties carry real weight here. The precedence order is a contract an
operator relies on when a unit's ``Environment=`` has to beat the file; and
the pinning of ``httpx`` and ``httpcore`` is a security property, because
those loggers print request headers -- meaning ``GITHUB_TOKEN`` -- at DEBUG,
and ``--log-level DEBUG`` is the first thing anyone reaches for in an
incident.
"""

import logging
import re
from pathlib import Path

import pytest

from pr_review_agent import logs
from pr_review_agent.config import Config, ConfigError

#: The smallest document the loader accepts. `logging` is absent from it,
#: which is itself the assertion that the section is optional.
BASE = {
    "github": {"repo": "prasadtalasila/pr-review-agent", "agent_user_id": 42},
    "triggers": {"allowlist": [114395272]},
    "budget": {
        "session_tokens": 88000,
        "weekly_tokens": 1500000,
        "max_run_tokens": 60000,
    },
    "engine": {
        "model": "claude-sonnet-5",
        "expected_version": "2.1.274",
        "timeout_seconds": 900,
    },
}


@pytest.fixture(name="clean_logging")
def _clean_logging():
    """Undo whatever ``configure`` did to the process-wide logger tree."""
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    names = (logs.PACKAGE_LOGGER, *logs.THIRD_PARTY_FLOORS)
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    root.handlers, root.level = before
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


# --------------------------------------------------------------------------
# parse_level
# --------------------------------------------------------------------------


@pytest.mark.parametrize("given", ["debug", "DEBUG", " Debug "])
def test_a_level_is_named_case_insensitively(given):
    assert logs.parse_level(given, source="x") == "DEBUG"


@pytest.mark.parametrize("given", ["", "VERBOSE", "WARN", "FATAL", "10"])
def test_a_level_that_is_not_one_of_the_five_is_refused(given):
    """``WARN`` and ``FATAL`` are stdlib aliases no layer documents."""
    with pytest.raises(logs.LevelError):
        logs.parse_level(given, source="x")


def test_the_error_names_the_layer_that_has_the_typo():
    with pytest.raises(logs.LevelError, match="PR_REVIEW_AGENT_LOG_LEVEL"):
        logs.parse_level("VERBOSE", source="PR_REVIEW_AGENT_LOG_LEVEL")


# --------------------------------------------------------------------------
# Precedence: flag > environment > config file
# --------------------------------------------------------------------------


def test_the_flag_beats_the_environment_and_the_file(monkeypatch):
    monkeypatch.setenv(logs.LEVEL_ENV_VAR, "WARNING")
    assert logs.resolve_level("DEBUG", "ERROR") == "DEBUG"


def test_the_environment_beats_the_file(monkeypatch):
    monkeypatch.setenv(logs.LEVEL_ENV_VAR, "WARNING")
    assert logs.resolve_level(None, "ERROR") == "WARNING"


def test_the_file_is_used_when_nothing_overrides_it(monkeypatch):
    monkeypatch.delenv(logs.LEVEL_ENV_VAR, raising=False)
    assert logs.resolve_level(None, "ERROR") == "ERROR"


def test_an_empty_environment_variable_is_not_a_level(monkeypatch):
    """``Environment=PR_REVIEW_AGENT_LOG_LEVEL=`` falls through, not errors."""
    monkeypatch.setenv(logs.LEVEL_ENV_VAR, "")
    assert logs.resolve_level(None, "ERROR") == "ERROR"


def test_a_typo_in_the_environment_is_an_error_and_not_a_silent_default(
    monkeypatch,
):
    monkeypatch.setenv(logs.LEVEL_ENV_VAR, "VERBOSE")
    with pytest.raises(logs.LevelError):
        logs.resolve_level(None, "INFO")


# --------------------------------------------------------------------------
# configure
# --------------------------------------------------------------------------


def test_the_level_lands_on_this_package(clean_logging):
    logs.configure("DEBUG")
    assert logging.getLogger(logs.PACKAGE_LOGGER).isEnabledFor(logging.DEBUG)


@pytest.mark.parametrize("noisy", logs.THIRD_PARTY_FLOORS)
def test_debug_does_not_turn_on_the_transport_or_bury_the_review_log(
    clean_logging, noisy
):
    """`httpcore` prints fourteen records per request at DEBUG, including the
    response header list, and `httpx` a line per request at INFO. Neither is
    reachable by asking the agent for DEBUG."""
    logs.configure("DEBUG")
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.DEBUG)


def test_the_default_level_leaves_all_three_silent(clean_logging):
    """The noise this replaces: `basicConfig(level=INFO)` on the root logger
    put `httpx`'s per-request line into the log on every poll."""
    logs.configure("INFO")
    # Each is held above the level at which it actually emits: `httpx` logs
    # only at INFO, `httpcore` and `asyncio` only at DEBUG.
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("asyncio").isEnabledFor(logging.DEBUG)


def test_asyncio_follows_the_level_because_it_has_nothing_to_hold_back(
    clean_logging,
):
    """One record for the whole process, and no credential near it."""
    logs.configure("DEBUG")
    assert logging.getLogger("asyncio").isEnabledFor(logging.DEBUG)


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ("DEBUG", {"httpx": "WARNING", "httpcore": "INFO", "asyncio": "DEBUG"}),
        ("INFO", {"httpx": "WARNING", "httpcore": "INFO", "asyncio": "INFO"}),
        (
            "WARNING",
            {"httpx": "WARNING", "httpcore": "WARNING", "asyncio": "WARNING"},
        ),
        ("ERROR", {"httpx": "ERROR", "httpcore": "ERROR", "asyncio": "ERROR"}),
        (
            "CRITICAL",
            {"httpx": "CRITICAL", "httpcore": "CRITICAL", "asyncio": "CRITICAL"},
        ),
    ],
)
def test_each_library_has_its_own_floor_and_follows_the_level_below_it(asked, expected):
    """A floor each, not one shared pin: the three get loud at three
    different levels, so they are held at three different ones. Asking for
    ERROR silences all of them, which a pin at WARNING would not have."""
    assert {
        name: logging.getLevelName(logs._third_party_level(name, asked))
        for name in logs.THIRD_PARTY_FLOORS
    } == expected


@pytest.mark.parametrize("noisy", logs.THIRD_PARTY_FLOORS)
def test_asking_for_critical_silences_third_party_errors_too(clean_logging, noisy):
    logs.configure("CRITICAL")
    assert not logging.getLogger(noisy).isEnabledFor(logging.ERROR)
    assert logging.getLogger(noisy).isEnabledFor(logging.CRITICAL)


# --------------------------------------------------------------------------
# The config-file layer
# --------------------------------------------------------------------------


def test_the_section_is_optional_and_defaults_to_info():
    assert Config.from_mapping(BASE).logging.level == logs.DEFAULT_LEVEL == "INFO"


def test_a_level_in_the_file_is_read_and_canonicalised():
    config = Config.from_mapping({**BASE, "logging": {"level": "debug"}})
    assert config.logging.level == "DEBUG"


def test_an_unknown_level_in_the_file_is_refused_at_startup():
    with pytest.raises(ConfigError, match="logging.level"):
        Config.from_mapping({**BASE, "logging": {"level": "VERBOSE"}})


def test_an_unknown_key_in_the_section_is_refused():
    """No destinations and no per-logger map -- see ``docs/LOGGING.md``."""
    with pytest.raises(ConfigError, match="loggers"):
        Config.from_mapping(
            {**BASE, "logging": {"level": "INFO", "loggers": {"x": "DEBUG"}}}
        )


# --------------------------------------------------------------------------
# The six events, and the level each is emitted at
# --------------------------------------------------------------------------
#
# `docs/LOGGING.md` names six things an operator wants visible while the
# daemon runs. The level each one carries is the contract between that page
# and the code, and it is spread over four modules -- so it is pinned here,
# in one place, by reading the levels out of the source rather than by
# running a daemon.

SIX_EVENTS = [
    ("1 poll cycle", "daemon.py", '"cycle seen=%d enqueued=%d"', "debug"),
    (
        "2 trigger decision",
        "triggers/classifier.py",
        '"trigger decision kind=%s repo=%s pr=%s reason=%s"',
        "debug",
    ),
    ("3 review start", "worker.py", '"reviewing %s#%d as %s (mode=%s)"', "info"),
    (
        "4 review outcome",
        "worker.py",
        '"reviewed %s: %s, %d findings, %d tokens"',
        "info",
    ),
    (
        "5 review posted",
        "publisher.py",
        '"published %s on %s#%d as comment %d"',
        "info",
    ),
    (
        "6 remaining budget",
        "worker.py",
        '"budget after %s: %d tokens left in the %s window, mode=%s"',
        "error",
    ),
]

_SRC = Path(logs.__file__).parent


@pytest.mark.parametrize(
    ("event", "module", "message", "level"),
    SIX_EVENTS,
    ids=[e[0] for e in SIX_EVENTS],
)
def test_each_of_the_six_events_is_emitted_at_its_agreed_level(
    event, module, message, level
):
    source = (_SRC / module).read_text(encoding="utf-8")
    assert message in source, f"{event}: the record is gone from {module}"
    call = re.search(r"logger\.(\w+)\(\s*" + re.escape(message), source)
    assert call, f"{event}: no logger call found for the record in {module}"
    assert call.group(1) == level, (
        f"{event}: emitted at logger.{call.group(1)}, agreed level is {level}"
    )


def test_the_first_two_events_are_hidden_at_the_default_level(clean_logging):
    """Events 1 and 2 fire per poll cycle and per pull request; 3 to 6 fire
    per review. Only the second group is visible without asking."""
    logs.configure(logs.DEFAULT_LEVEL)
    package = logging.getLogger(logs.PACKAGE_LOGGER)
    assert not package.isEnabledFor(logging.DEBUG)
    assert package.isEnabledFor(logging.INFO)


def test_the_budget_readout_survives_the_quietest_useful_level(clean_logging):
    """Event 6 alone sits at ERROR, so `--log-level ERROR` still answers
    "what is left to spend" after every review."""
    logs.configure("ERROR")
    package = logging.getLogger(logs.PACKAGE_LOGGER)
    assert package.isEnabledFor(logging.ERROR)
    assert not package.isEnabledFor(logging.WARNING)
