"""Check a rendered review report against the contract it claims to follow.

``render_report.py`` cannot produce a report that fails this; the case this
exists for is the report somebody wrote by hand, or edited after rendering.
Both happen -- a review filed as issues, a comment tidied before posting --
and both are how a report acquires a renumbered list or a lost trailer.

Every rule is named for the bold identifier in
``references/report-contract.md``, so a failure points at the paragraph that
explains itself. The constants come from ``report`` rather than being
restated, because a checker that has its own idea of the trailer is a second
source of truth for the thing it is policing.

One rule on that page is not checked here: **no-verdict**. "This looks fine
to merge" is a sentence, not a shape, and a checker that pattern-matched for
it would fire on a finding that quotes one. It stays a rule a reader enforces.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Printed instead of a traceback when the package is not importable.
#: ``skill install`` copies files into a skills directory; it does not
#: install anything into the interpreter that then runs them, and the two
#: are routinely different -- a CLI living in a pipx or poetry environment
#: is not on the ``python3`` a session reaches for. A bare
#: ``ModuleNotFoundError`` on an import line does not say that, and the
#: reader's next guess is that the skill installed wrongly.
MISSING_PACKAGE = """\
{script} cannot import `pr_review_agent`, and there is no copy of it
beside this script either. This interpreter is:

    {executable}

Either re-run

    pr-review-agent skill install --force

which copies the renderer into the skill directory, or install the package
into the interpreter above:

    pip install pr-review-agent

{what}
It is imported rather than reimplemented so that a report written by hand
and one the agent posts cannot say the same findings differently.

`collect_context.py` is stdlib-only and works without any of this.\
"""


def _missing(script: str, what: str) -> SystemExit:
    """The message above, filled in, as the exception to raise."""
    return SystemExit(
        MISSING_PACKAGE.format(script=script, executable=sys.executable, what=what)
    )


#: The copy ``skill install`` leaves beside this script, for a machine that
#: has the skill and no ``pr_review_agent``. Appended rather than inserted:
#: an installed package takes priority, so upgrading the package upgrades
#: the renderer even when the skill directory is older than it is. Absent
#: when the script is run out of a source checkout, where the path is a
#: no-op and the package is importable anyway.
sys.path.append(str(Path(__file__).resolve().parent / "_vendor"))

try:
    from pr_review_agent.report import (
        MAX_BODY_CHARS,
        SECTIONS,
        TRAILER,
        TRUNCATION_NOTE,
    )
except ModuleNotFoundError as missing:  # pragma: no cover - see test_skill.py
    raise _missing(
        "check_report.py",
        "This script reads `MAX_BODY_CHARS`, `SECTIONS`, `TRAILER` and\n"
        "`TRUNCATION_NOTE` out of `report`; the rules it enforces are\n"
        "the renderer's own.\n",
    ) from missing

HEADER = re.compile(
    r"\A## Review: PR #\d+ — round \d+ \(`[0-9a-f]{7}`, \d+ commits\)\Z"
)
HEADING = re.compile(r"\A## (.+)\Z")
ENTRY = re.compile(r"\A(\d+)\. \*\*(.+)\*\*\Z")
LISTED = re.compile(r"\A\s*(?:\d+\.|[-*+])\s")
ORDER = [heading for heading, _ in SECTIONS]


@dataclass(frozen=True)
class Report:
    """A rendered report, and the same text split into lines.

    One argument rather than two so that every check has the same signature
    and ``CHECKS`` can be a plain tuple -- and so that a check which needs
    only one of them does not have to declare an argument it ignores.
    """

    text: str
    lines: list[str]

    @property
    def headings(self) -> list[str]:
        """Every ``## `` heading after the header line."""
        return [m.group(1) for line in self.lines[1:] if (m := HEADING.match(line))]

    def section(self, heading: str) -> list[str]:
        """The lines under ``## <heading>``, to the next heading or the trailer."""
        try:
            start = self.lines.index(f"## {heading}") + 1
        except ValueError:
            return []
        body = []
        for line in self.lines[start:]:
            if HEADING.match(line) or line in (TRAILER, TRUNCATION_NOTE):
                break
            body.append(line)
        return body


def check_header(report: Report) -> list[str]:
    """**header** -- the first line names the PR, round, sha7 and commit count."""
    if not report.lines or not HEADER.match(report.lines[0]):
        return ["header: first line must name the PR, round, sha7 and commit count"]
    return []


def check_sections(report: Report) -> list[str]:
    """**sections** -- only the three known headings, each once, in order."""
    found = report.headings
    unknown = [h for h in found if h not in ORDER]
    if unknown:
        return [f"sections: unknown heading(s) {unknown}; allowed are {ORDER}"]
    ranks = [ORDER.index(h) for h in found]
    if ranks != sorted(set(ranks)):
        return [f"sections: must appear once each in the order {ORDER}, got {found}"]
    return []


def check_numbering(report: Report) -> list[str]:
    """**numbering** -- one ascending sequence across the whole report."""
    numbers = [int(m.group(1)) for line in report.lines if (m := ENTRY.match(line))]
    if numbers != sorted(set(numbers)):
        return [f"numbering: one ascending sequence across the report, got {numbers}"]
    return []


def check_nits_are_prose(report: Report) -> list[str]:
    """**nits-are-prose** -- nothing numbered or bulleted under Nits."""
    listed = [line for line in report.section("Nits") if LISTED.match(line)]
    if listed:
        return [
            f"nits-are-prose: {len(listed)} numbered or bulleted line(s) under Nits"
        ]
    return []


def check_empty_report(report: Report) -> list[str]:
    """**empty-report** -- a report with no sections says so, and stops."""
    if report.headings:
        return []
    middle = [line for line in report.lines[1:] if line.strip() and line != TRAILER]
    if middle != ["No issues found."]:
        return ["empty-report: a report with no sections says 'No issues found.'"]
    return []


def check_trailer(report: Report) -> list[str]:
    """**trailer** -- verbatim, last, with nothing after it."""
    tail = [line for line in report.lines if line.strip()]
    if not tail or tail[-1] != TRAILER:
        return ["trailer: must be the last line, verbatim, with nothing after it"]
    return []


def check_length(report: Report) -> list[str]:
    """**length** -- within the comment size the publisher renders to."""
    if len(report.text) > MAX_BODY_CHARS:
        size = len(report.text)
        return [f"length: {size} chars exceeds MAX_BODY_CHARS ({MAX_BODY_CHARS})"]
    return []


CHECKS = (
    check_header,
    check_sections,
    check_numbering,
    check_nits_are_prose,
    check_empty_report,
    check_trailer,
    check_length,
)


def violations(text: str) -> list[str]:
    """Every rule the report breaks, in the order the rules are listed."""
    report = Report(text, text.rstrip("\n").splitlines())
    return [problem for check in CHECKS for problem in check(report)]


def main(argv: list[str] | None = None) -> int:
    """Print every violation to stderr; exit 1 if there were any."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("report", type=Path, help="a rendered report; - for stdin")
    args = parser.parse_args(argv)
    text = (
        sys.stdin.read()
        if str(args.report) == "-"
        else args.report.read_text(encoding="utf-8")
    )
    found = violations(text)
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
