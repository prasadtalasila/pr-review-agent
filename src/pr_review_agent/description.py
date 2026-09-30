"""What ``@claude describe`` produces, and how it is laid out as a comment.

A description is a pull request summary in a fixed shape: what kind of
change it is, a paragraph saying what it does, one sentence per changed
file, and how to test it. The shape is pr-agent's ``/describe`` walkthrough
(MIT), cut to the parts a reader copies into a pull request body.

**It is posted as a comment and never written into the pull request.** The
publisher's write set is a reaction and one comment, and editing a body a
contributor wrote is a different kind of write -- one that loses their text
if the model is wrong. A maintainer copies what is useful.

Poor on purpose, like :mod:`.report`, whose helpers it shares: ``skill
install`` copies this module so the ``pr-description`` skill renders through
the same code the daemon posts with. Every field is engine output over an
untrusted tree, so every one goes through ``sanitise`` before it is laid out.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._compat import StrEnum
from .report import MAX_BODY_CHARS, _code
from .sanitise import sanitise
from .triggers.mention import neutralise


class ChangeType(StrEnum):
    """What kind of change a pull request is, as pr-agent names them."""

    BUG_FIX = "bug_fix"
    ENHANCEMENT = "enhancement"
    REFACTOR = "refactor"
    DOCUMENTATION = "documentation"
    TESTS = "tests"
    OTHER = "other"


#: How each type reads in the comment.
LABELS = {
    ChangeType.BUG_FIX: "Bug fix",
    ChangeType.ENHANCEMENT: "Enhancement",
    ChangeType.REFACTOR: "Refactor",
    ChangeType.DOCUMENTATION: "Documentation",
    ChangeType.TESTS: "Tests",
    ChangeType.OTHER: "Other",
}

#: Said once, on every description. Like the review's trailer, it says the
#: agent did nothing beyond this comment -- and that the pull request body is
#: still the contributor's.
DESCRIPTION_TRAILER = (
    "<sub>Automated description. It takes no action on this pull request "
    "beyond this comment; copy what is useful into the description.</sub>"
)


@dataclass(frozen=True)
class FileChange:
    """One changed file and one sentence about it."""

    path: str
    change: str


@dataclass(frozen=True)
class Description:
    """A pull request described in the fixed shape."""

    type: ChangeType
    summary: str
    files: tuple[FileChange, ...]
    testing: str

    def to_json(self) -> dict:
        """The description as the engine's schema spells it."""
        return {
            "type": str(self.type),
            "summary": self.summary,
            "files": [{"path": f.path, "change": f.change} for f in self.files],
            "testing": self.testing,
        }

    @classmethod
    def from_json(cls, data: dict) -> Description:
        """Read one back; ``KeyError``/``ValueError`` if it does not fit."""
        return cls(
            type=ChangeType(data["type"]),
            summary=str(data["summary"]),
            files=tuple(
                FileChange(path=str(f["path"]), change=str(f["change"]))
                for f in data["files"]
            ),
            testing=str(data["testing"]),
        )


def render_description(
    head_sha: str,
    description: Description,
    *,
    pr_number: int,
    commits: int,
    handle: str,
    moved_to: str | None = None,
) -> str:
    """The comment body for a description of ``head_sha``.

    Files beyond what fits a GitHub comment are dropped from the end of the
    table and counted, so a very wide pull request still gets its summary
    and its test plan rather than a 422.
    """
    # All but two keyword-only, as `report.render`'s are.
    # pylint: disable=too-many-arguments
    header = f"## Description: PR #{pr_number} (`{head_sha[:7]}`, {commits} commits)"
    if moved_to is not None:
        header += (
            f"\n\n_This describes `{head_sha[:7]}`, which is no longer the head: "
            f"the branch has since moved to `{moved_to[:7]}`._"
        )
    rows = [_row(f) for f in description.files]
    for kept in range(len(rows), -1, -1):
        body = _assemble(header, description, rows[:kept], len(rows) - kept)
        if len(body) <= MAX_BODY_CHARS:
            break
    return neutralise(body[:MAX_BODY_CHARS], handle)


def _assemble(
    header: str, description: Description, rows: list[str], dropped: int
) -> str:
    """Header, type, summary, the file table, the test plan, the trailer."""
    table = "| File | Change |\n| :-- | :-- |\n" + "\n".join(rows)
    if dropped:
        table += f"\n\n_And {dropped} more changed files, left out for length._"
    parts = [
        header,
        f"**Type:** {LABELS[description.type]}",
        sanitise(description.summary.strip()),
        "## Changes",
        table if rows or dropped else "_No changed files were described._",
        "## How to test",
        sanitise(description.testing.strip()),
        DESCRIPTION_TRAILER,
    ]
    return "\n\n".join(parts)


def _row(change: FileChange) -> str:
    """One table row: a pipe or a newline in either cell would end it."""
    return f"| {_cell(_code(change.path))} | {_cell(sanitise(change.change))} |"


def _cell(text: str) -> str:
    """``text`` made safe to sit inside one table cell."""
    return " ".join(text.split()).replace("|", "\\|")
