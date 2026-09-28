"""The shape of a record: which formatter is chosen, and what it emits.

Split from ``test_logs.py``, which keeps the other half -- who sets the
level, and what is logged at which one. The seam is real: everything
here is about the bytes that reach a stream, and nothing here decides
whether a record is emitted at all.

The ``<N>`` prefix is the property worth the most: it is what makes
``journalctl -p warning`` answer, and emitting it wrongly would make
every line of a JSON log file unparseable.
"""

import io
import json
import logging
import os
import re
from datetime import datetime, timezone

import pytest
from logs_harness import SRC

from pr_review_agent import logs
from pr_review_agent.triggers import Actor, Allowlist, Classifier, PullRequest

# --------------------------------------------------------------------------
# The shape of a record: auto, text, json
# --------------------------------------------------------------------------
#
# Two decisions, resolved by two different tests and deliberately not
# conflated: the shape follows `isatty` -- human-readable when a human is
# watching -- and the `<N>` prefix follows journald detection. `isatty` is
# false for a file, a pipe, a container runtime *and* the journal, so it
# cannot drive the prefix.


class _Stream(io.StringIO):
    """A stderr stand-in that answers `isatty` however the test needs."""

    def __init__(self, *, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        # No descriptor, so `_on_journal` cannot match -- which is what the
        # prefix tests below supply a real file for.
        raise io.UnsupportedOperation("fileno")


def _configured_formatter(monkeypatch, stream, fmt) -> logging.Formatter:
    """The formatter `configure` installs when stderr is `stream`."""
    monkeypatch.setattr("sys.stderr", stream)
    logs.configure(logs.DEFAULT_LEVEL, fmt)
    formatter = logging.getLogger().handlers[-1].formatter
    assert formatter is not None
    return formatter


@pytest.mark.usefixtures("clean_logging")
@pytest.mark.parametrize(
    ("tty", "expected"),
    [(True, logging.Formatter), (False, logs.JsonFormatter)],
    ids=["a human is watching", "a collector is"],
)
def test_auto_resolves_by_whether_stderr_is_a_terminal(monkeypatch, tty, expected):
    formatter = _configured_formatter(monkeypatch, _Stream(tty=tty), "auto")
    # `type` and not `isinstance`: JsonFormatter subclasses Formatter, so
    # isinstance passes for both and the parametrisation asserts nothing.
    assert type(formatter) is expected  # pylint: disable=unidiomatic-typecheck


@pytest.mark.usefixtures("clean_logging")
@pytest.mark.parametrize("tty", [True, False], ids=["terminal", "pipe"])
def test_a_named_format_is_not_second_guessed_by_the_terminal(monkeypatch, tty):
    # See above on `type` rather than `isinstance`.
    # pylint: disable=unidiomatic-typecheck
    asked_for_json = _configured_formatter(monkeypatch, _Stream(tty=tty), "json")
    asked_for_text = _configured_formatter(monkeypatch, _Stream(tty=tty), "text")
    assert type(asked_for_json) is logs.JsonFormatter
    assert type(asked_for_text) is logging.Formatter


@pytest.mark.usefixtures("clean_logging")
def test_passing_nothing_is_still_a_line_of_text_on_a_terminal(monkeypatch):
    """The no-change case: an operator who passes no flag and runs the daemon
    by hand sees exactly what they saw before this existed."""
    stream = _Stream(tty=True)
    formatter = _configured_formatter(monkeypatch, stream, logs.DEFAULT_FORMAT)
    record = logging.LogRecord(
        "pr_review_agent.worker", logging.INFO, "x.py", 1, "reviewing r#1", (), None
    )
    assert formatter.format(record).endswith(
        "INFO pr_review_agent.worker reviewing r#1"
    )


# --------------------------------------------------------------------------
# journald detection and the `<N>` priority prefix
# --------------------------------------------------------------------------


@pytest.fixture(name="journal")
def _journal(tmp_path, monkeypatch):
    """A real file standing in for the journal socket, with JOURNAL_STREAM
    naming its device and inode -- which is exactly what systemd does.

    A file rather than a socket because `os.fstat` is the whole test, and
    `st_dev` and `st_ino` are populated on all three CI platforms.
    """
    path = tmp_path / "journal"
    with path.open("w", encoding="utf-8") as stream:
        st = os.fstat(stream.fileno())
        monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
        yield stream


def test_the_journal_is_recognised_by_device_and_inode(journal):
    assert logs._on_journal(journal)


@pytest.mark.usefixtures("journal")
def test_a_child_that_inherited_the_variable_is_not_the_journal(tmp_path):
    """The engine adapter spawns `claude` and the workspace spawns `git`,
    both with a pipe for stderr and both carrying JOURNAL_STREAM. A
    presence-only check would prefix their output too."""
    with (tmp_path / "pipe").open("w", encoding="utf-8") as other:
        assert not logs._on_journal(other)


def test_no_journal_variable_is_no_journal(journal, monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM")
    assert not logs._on_journal(journal)


def _rendered(monkeypatch, stream, level):
    """One record of `level` as `configure` would write it to `stream`."""
    formatter = _configured_formatter(monkeypatch, stream, "json")
    return formatter.format(
        logging.LogRecord(
            "pr_review_agent.budget", level, "x.py", 1, "paused", (), None
        )
    )


@pytest.mark.usefixtures("clean_logging")
def test_a_warning_under_journald_carries_the_priority_journald_strips(
    monkeypatch, journal
):
    """Without this every record is stored at PRIORITY=6 and
    `journalctl -u pr-review-agent -p warning` returns nothing, ever."""
    line = _rendered(monkeypatch, journal, logging.WARNING)
    assert line.startswith("<4>")
    assert json.loads(line[3:])["level"] == "warning"


@pytest.mark.usefixtures("clean_logging")
def test_the_prefix_is_absent_from_a_file_or_a_pipe(monkeypatch):
    """Prefixing a file would make every line invalid JSON, silently."""
    line = _rendered(monkeypatch, _Stream(tty=False), logging.WARNING)
    assert line.startswith("{")
    assert json.loads(line)["level"] == "warning"


@pytest.mark.usefixtures("clean_logging")
@pytest.mark.parametrize(("level", "digit"), sorted(logs.PRIORITIES.items()))
def test_every_level_maps_to_its_syslog_priority(monkeypatch, journal, level, digit):
    assert _rendered(monkeypatch, journal, level).startswith(f"<{digit}>")


# --------------------------------------------------------------------------
# The JSON record: contextual values as fields, not as interpolated text
# --------------------------------------------------------------------------


def _one_json_record(caplog, emit, level=logging.DEBUG):
    """`emit()`'s single record, rendered as `JsonFormatter` would write it."""
    with caplog.at_level(level, logger=logs.PACKAGE_LOGGER):
        emit()
    (record,) = caplog.records
    return json.loads(logs.JsonFormatter().format(record))


def test_a_trigger_decision_is_queryable_by_reason_and_pull_request(caplog):
    """Event 2 is the answer to "why wasn't this reviewed", and the whole
    point of the JSON shape is that the answer is a field:

        jq -r 'select(.reason) | [.pr, .reason] | @tsv'
    """
    classifier = Classifier(
        allowlist=Allowlist.from_config([1234]),
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    pull = PullRequest(
        repo="prasadtalasila/pr-review-agent",
        number=7,
        head_sha="abc123",
        author=Actor(user_id=5555, login="outsider"),
        created_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        is_draft=False,
    )

    payload = _one_json_record(caplog, lambda: classifier.classify_pull_request(pull))

    assert payload["reason"] == "author_not_allowlisted"
    assert payload["pr"] == 7
    assert payload["repo"] == "prasadtalasila/pr-review-agent"
    assert payload["kind"] == "pr_opened"
    assert payload["level"] == "debug"
    assert payload["logger"] == "pr_review_agent.triggers.classifier"


def test_the_message_still_reads_as_a_sentence(caplog):
    """The fields are added beside the text, not instead of it: text mode is
    the same line it was, and `msg` in JSON mode is still legible."""
    logger = logging.getLogger(f"{logs.PACKAGE_LOGGER}.worker")
    payload = _one_json_record(
        caplog,
        lambda: logger.info(
            "reviewing %s#%d", "o/r", 7, extra={"repo": "o/r", "pr": 7}
        ),
        level=logging.INFO,
    )
    assert payload["msg"] == "reviewing o/r#7"


def test_a_traceback_is_one_record_and_not_several(caplog):
    """journald applies the priority prefix per line, so a traceback spread
    over several lines would keep its priority only on the first."""
    logger = logging.getLogger(f"{logs.PACKAGE_LOGGER}.worker")

    def emit():
        try:
            raise RuntimeError("engine would not start")
        except RuntimeError:
            logger.error("giving up on %s permanently", "k", exc_info=True)

    payload = _one_json_record(caplog, emit, level=logging.ERROR)
    assert "RuntimeError: engine would not start" in payload["exc"]


@pytest.mark.parametrize(
    ("event", "module", "message", "keys"),
    [
        (
            "2 trigger decision",
            "triggers/classifier.py",
            '"trigger decision kind=%s repo=%s pr=%s reason=%s"',
            ("kind", "repo", "pr", "reason"),
        ),
        (
            "4 review outcome",
            "worker.py",
            '"reviewed %s: %s, %d findings, %d tokens"',
            ("findings", "tokens"),
        ),
        (
            "6 remaining budget",
            "worker.py",
            '"budget after %s: %d tokens left in the %s window, mode=%s"',
            ("remaining", "tightest"),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_documented_query_keys_are_attached_where_they_are_emitted(
    event, module, message, keys
):
    """`docs/LOGGING.md` queries `.pr`, `.reason`, `.remaining` and
    `.tightest` by name. Read out of the source, so the page and the code
    cannot drift apart."""
    source = (SRC / module).read_text(encoding="utf-8")
    call = re.search(re.escape(message) + r".*?\n\s*\)", source, re.DOTALL)
    assert call, f"{event}: the record is gone from {module}"
    for key in keys:
        assert f'"{key}":' in call.group(0), f"{event}: no {key} field in {module}"
