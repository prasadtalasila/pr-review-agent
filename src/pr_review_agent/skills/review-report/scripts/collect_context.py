"""Header facts for a review report, read out of git rather than guessed.

``publisher.render`` puts the head sha, the round and the commit count in
every report's first line, because the comment is edited in place and a
reader has to be able to tell which revision the text describes. Writing a
report by hand means supplying those four values by hand, and the sha is the
one nobody remembers: a report that quotes the wrong one is worse than no
header at all, since it claims to describe a revision it never read.

So this prints them, as JSON that ``render_report.py --context`` consumes.
It runs git and nothing else -- no network, no GitHub, no store.

``--round`` cannot come from git. It counts *recorded runs* against this pull
request, which only the daemon's store knows; for a review written by hand it
is whatever the last report said plus one, and it defaults to 1.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    """One git command's stdout, stripped. Non-zero exit raises."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def context(repo: Path, base: str, head: str, pr: int, round_number: int) -> dict:
    """The header facts, plus the paths the diff touches.

    ``files`` is the sweep's starting point, not its extent: the scope rule
    admits findings on files the diff never opens, so this is what to read
    first rather than what to read only.
    """
    head_sha = _git(repo, "rev-parse", head)
    merge_base = _git(repo, "merge-base", base, head_sha)
    changed = _git(repo, "diff", "--name-only", f"{merge_base}..{head_sha}")
    return {
        "pr": pr,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "commits": int(_git(repo, "rev-list", "--count", f"{merge_base}..{head_sha}")),
        "round": round_number,
        "files": changed.splitlines(),
    }


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", default="origin/main", help="what to diff against")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument(
        "--round",
        type=int,
        default=1,
        dest="round_number",
        help="which review round this is; git cannot know it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Print the header facts as JSON."""
    args = _parse(argv)
    facts = context(args.repo, args.base, args.head, args.pr, args.round_number)
    print(json.dumps(facts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
