"""Check a rendered pull request description against its contract.

``render_description.py`` cannot produce one that fails this; the case this
exists for is a description written or edited by hand. Every rule is named
for the bold identifier in ``references/layout-contract.md``, and the
constants come from ``description`` rather than being restated, so the
checker has no second idea of the trailer.

**describe-not-review** is not checked. "This looks ready to merge" is a
sentence rather than a shape, and it stays a rule a reader enforces.
"""

from __future__ import annotations

import argparse
import re
import sys
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
and one the agent posts cannot lay the same description out differently.

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
    from pr_review_agent.description import DESCRIPTION_TRAILER, LABELS
    from pr_review_agent.report import MAX_BODY_CHARS
except ModuleNotFoundError as missing:  # pragma: no cover - see test_skill_describe.py
    raise _missing(
        "check_description.py",
        "This script reads `DESCRIPTION_TRAILER`, `LABELS` and\n"
        "`MAX_BODY_CHARS`; the rules it enforces are the renderer's own.\n",
    ) from missing

HEADER = re.compile(r"\A## Description: PR #\d+ \(`[0-9a-f]{7}`, \d+ commits\)\Z")
TYPE = re.compile(r"\A\*\*Type:\*\* (.+)\Z")
HEADINGS = ["Changes", "How to test"]
TABLE_HEAD = ["| File | Change |", "| :-- | :-- |"]


def check_header(lines: list[str]) -> list[str]:
    """**header** -- the first line names the PR, sha7 and commit count."""
    if not lines or not HEADER.match(lines[0]):
        return ["header: first line must name the PR, sha7 and commit count"]
    return []


def check_type(lines: list[str]) -> list[str]:
    """**type** -- one ``**Type:**`` line naming a known type."""
    found = [m.group(1) for line in lines if (m := TYPE.match(line))]
    if len(found) != 1 or found[0] not in LABELS.values():
        return [f"type: one **Type:** line, one of {sorted(LABELS.values())}"]
    return []


def check_headings(lines: list[str]) -> list[str]:
    """**headings** -- Changes then How to test, each once."""
    found = [line[3:] for line in lines[1:] if line.startswith("## ")]
    if found != HEADINGS:
        return [f"headings: expected {HEADINGS}, got {found}"]
    return []


def check_table(lines: list[str]) -> list[str]:
    """**table** -- under Changes, a two-column table or the no-files line."""
    try:
        start = lines.index("## Changes") + 1
    except ValueError:
        return []
    body = [line for line in lines[start:] if line.strip()]
    if body[:1] == ["_No changed files were described._"]:
        return []
    rows = body[2:]
    rows = rows[: next((i for i, r in enumerate(rows) if not r.startswith("|")), None)]
    if body[:2] != TABLE_HEAD or not rows:
        return ["table: Changes holds a | File | Change | table with one row per file"]
    return []


def check_trailer(lines: list[str]) -> list[str]:
    """**trailer** -- verbatim, last, with nothing after it."""
    tail = [line for line in lines if line.strip()]
    if not tail or tail[-1] != DESCRIPTION_TRAILER:
        return ["trailer: must be the last line, verbatim, with nothing after it"]
    return []


CHECKS = (check_header, check_type, check_headings, check_table, check_trailer)


def violations(text: str) -> list[str]:
    """Every rule the description breaks, in the order the rules are listed."""
    lines = text.rstrip("\n").splitlines()
    found = [problem for rule in CHECKS for problem in rule(lines)]
    if len(text) > MAX_BODY_CHARS:
        found.append(f"length: {len(text)} chars exceeds {MAX_BODY_CHARS}")
    return found


def main(argv: list[str] | None = None) -> int:
    """Print every violation to stderr; exit 1 if there were any."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("description", type=Path, help="a rendered file; - for stdin")
    args = parser.parse_args(argv)
    text = (
        sys.stdin.read()
        if str(args.description) == "-"
        else args.description.read_text(encoding="utf-8")
    )
    found = violations(text)
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
