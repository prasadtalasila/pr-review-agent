"""Header facts for a review report, read out of git rather than guessed.

``report.render`` puts the head sha, the round and the commit count in
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

It also answers a question the header does not ask: which of the reviewed
repository's own standards files exist **at the merge base**. The daemon has
that channel already -- ``engine.standards_paths``, read by
``engine/standards.py`` at the merge base -- and a person running the skill
had none. Naming them here puts the merge-base rule somewhere that executes,
rather than in a paragraph the reviewer may or may not act on.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

#: Where a repository conventionally writes down what it expects of a
#: change, when no ``pr-review-agent`` config names something else. Read at
#: the merge base like everything else here, never at the head: a pull
#: request that can rewrite the reviewer's instructions has talked its way
#: past the controls. ``docs/CONFIG.md`` makes the argument in full, and
#: what it protects the daemon from it protects a person from too -- an
#: interactive reviewer works in a tree checked out at the *head*, where
#: nothing applies that rule for them.
DEFAULT_STANDARDS = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")


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


def standards(repo: Path, merge_base: str, candidates: tuple[str, ...]) -> list[str]:
    """Which of ``candidates`` exist at the merge base.

    Names them rather than reading them: what they say is for the reviewer
    to read, and the value here is the *revision* -- ``git show
    <merge_base>:<path>``, not the copy in the working tree, which is the
    pull request's and may have been edited by it.

    A candidate that is not there is skipped. A repository need not carry
    every conventional file, and an absent one is not a failure.
    """
    present = []
    for path in candidates:
        try:
            _git(repo, "cat-file", "-e", f"{merge_base}:{path}")
        except subprocess.CalledProcessError:
            continue
        present.append(path)
    return present


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
    parser.add_argument(
        "--standards",
        action="append",
        metavar="PATH",
        help=(
            "a standards file to look for at the merge base; repeatable. "
            f"Defaults to {', '.join(DEFAULT_STANDARDS)}. Pass the paths a "
            "pr-review-agent config.yaml names, if the repository has one."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Print the header facts as JSON."""
    args = _parse(argv)
    facts = context(args.repo, args.base, args.head, args.pr, args.round_number)
    facts["standards"] = standards(
        args.repo, facts["merge_base"], tuple(args.standards or DEFAULT_STANDARDS)
    )
    print(json.dumps(facts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
