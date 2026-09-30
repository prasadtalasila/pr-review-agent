"""``findings.json`` in, a review report out, through the daemon's renderer.

The point is that it is the *same* renderer. Which heading a severity falls
under, where the numbering restarts (it does not), what order entries take
and what the trailer says are decided in ``report.render``, where a test
reads them -- so a report written by hand here and a report posted by the
agent cannot say the same findings differently.

Nothing here posts anything. It writes a file, or prints to stdout.

Numbering: ``--high-water`` is the largest number this pull request has ever
issued, which is not the same as the largest number still present. A finding
that was fixed takes its number out of circulation and leaves a gap, and the
gap is the report's way of saying so. The default -- the largest number in
the input -- is right for a first render and wrong for any round after one
where something was fixed, so pass the real figure when re-rendering.
"""

from __future__ import annotations

import argparse
import json
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
    from pr_review_agent.findings import (
        Assessment,
        Finding,
        Recommendation,
        Risk,
        Severity,
    )
    from pr_review_agent.numbering import assign
    from pr_review_agent.report import render
except ModuleNotFoundError as missing:  # pragma: no cover - see test_skill.py
    raise _missing(
        "render_report.py",
        "This script calls `report.render` and `numbering.assign`; the\n"
        "layout, the ordering and the numbering are decided there.\n",
    ) from missing


def findings_from(data: dict) -> tuple[Finding, ...]:
    """The ``findings`` array as ``Finding`` objects, order preserved."""
    return tuple(
        Finding(
            path=item["path"],
            line=item["line"],
            severity=Severity(item["severity"]),
            title=item["title"],
            body=item["body"],
            number=item.get("number"),
        )
        for item in data["findings"]
    )


def assessment_from(data: dict) -> Assessment:
    """The required ``assessment`` object, or a message saying what is wrong.

    Required here as it is of the daemon's reviewer: a report without one
    breaks the contract, so it is refused rather than rendered without it.
    """
    try:
        item = data["assessment"]
        return Assessment(
            effort=int(item["effort"]),
            risk=Risk(item["risk"]),
            recommendation=Recommendation(item["recommendation"]),
            priority_files=tuple(item["priority_files"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            "findings.json needs an `assessment` matching findings.schema.json: "
            f"{exc!r}"
        ) from exc


def _high_water(findings: tuple[Finding, ...], given: int | None) -> int:
    """What ``numbering.assign`` is told this pull request has issued."""
    if given is not None:
        return given
    return max((f.number or 0 for f in findings), default=0)


def _facts(args: argparse.Namespace) -> dict:
    """Header facts, from ``--context`` if given and from flags otherwise.

    Flags win over the file, so a context collected once can be re-used for a
    later round by overriding ``--round`` alone.
    """
    facts = json.loads(args.context.read_text(encoding="utf-8")) if args.context else {}
    for key in ("pr", "head_sha", "round", "commits"):
        value = getattr(args, key)
        if value is not None:
            facts[key] = value
    missing = [k for k in ("pr", "head_sha", "round", "commits") if k not in facts]
    if missing:
        sys.exit(f"missing header facts: {', '.join(missing)}")
    return facts


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("findings", type=Path, help="JSON matching findings.schema")
    parser.add_argument("--context", type=Path, help="collect_context.py output")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--head-sha", dest="head_sha")
    parser.add_argument("--round", type=int, dest="round")
    parser.add_argument("--commits", type=int)
    parser.add_argument("--high-water", type=int, help="largest number ever issued")
    parser.add_argument("--handle", default="claude", help="the agent's GitHub login")
    parser.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Render the report, to ``--out`` or to stdout."""
    args = _parse(argv)
    data = json.loads(args.findings.read_text(encoding="utf-8"))
    findings = findings_from(data)
    facts = _facts(args)
    body = render(
        facts["head_sha"],
        assign(findings, _high_water(findings, args.high_water)),
        pr_number=facts["pr"],
        round_number=facts["round"],
        commits=facts["commits"],
        handle=args.handle,
        assessment=assessment_from(data),
    )
    if args.out:
        args.out.write_text(body + "\n", encoding="utf-8")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
