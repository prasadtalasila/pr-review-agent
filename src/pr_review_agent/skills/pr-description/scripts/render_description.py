"""``description.json`` in, a pull request description out, through the
daemon's renderer.

The same renderer ``@claude describe`` posts with, so a description written
by hand and one the agent posts cannot lay the same fields out differently.
Nothing here posts anything, or edits a pull request. It writes a file, or
prints to stdout, and a person copies what is useful.
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
    from pr_review_agent.description import Description, render_description
except ModuleNotFoundError as missing:  # pragma: no cover - see test_skill_describe.py
    raise _missing(
        "render_description.py",
        "This script calls `description.render_description`; the layout,\n"
        "the table and the length cap are decided there.\n",
    ) from missing


def _facts(args: argparse.Namespace) -> dict:
    """Header facts, from ``--context`` if given; flags win over the file."""
    facts = json.loads(args.context.read_text(encoding="utf-8")) if args.context else {}
    for key in ("pr", "head_sha", "commits"):
        value = getattr(args, key)
        if value is not None:
            facts[key] = value
    missing = [k for k in ("pr", "head_sha", "commits") if k not in facts]
    if missing:
        sys.exit(f"missing header facts: {', '.join(missing)}")
    return facts


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("description", type=Path, help="JSON matching the schema")
    parser.add_argument("--context", type=Path, help="collect_context.py output")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--head-sha", dest="head_sha")
    parser.add_argument("--commits", type=int)
    parser.add_argument("--handle", default="claude", help="the agent's GitHub login")
    parser.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Render the description, to ``--out`` or to stdout."""
    args = _parse(argv)
    data = json.loads(args.description.read_text(encoding="utf-8"))
    facts = _facts(args)
    body = render_description(
        facts["head_sha"],
        Description.from_json(data),
        pr_number=facts["pr"],
        commits=facts["commits"],
        handle=args.handle,
    )
    if args.out:
        args.out.write_text(body + "\n", encoding="utf-8")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
