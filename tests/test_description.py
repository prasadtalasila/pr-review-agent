"""The description comment: engine prose made inert, and a body that fits.

Every field is written by a model about a tree an attacker can edit, so the
same rules hold as for a review: nothing it says may notify, cross-reference
or render HTML, the agent's own handle is neutralised, and GitHub's length
limit is met by dropping table rows rather than by a 422.
"""

from pr_review_agent.description import (
    DESCRIPTION_TRAILER,
    ChangeType,
    Description,
    FileChange,
    render_description,
)
from pr_review_agent.report import MAX_BODY_CHARS
from pr_review_agent.triggers.mention import has_mention

SHA = "0123456789abcdef0123456789abcdef01234567"


def _render(description: Description, **kwargs) -> str:
    return render_description(
        SHA, description, pr_number=7, commits=2, handle="claude", **kwargs
    )


def _described(**overrides) -> Description:
    base = {
        "type": ChangeType.BUG_FIX,
        "summary": "Fixes it.",
        "files": (FileChange(path="a.py", change="The fix."),),
        "testing": "Run pytest.",
    }
    return Description(**{**base, **overrides})


def test_the_layout_is_header_type_summary_table_testing_trailer():
    body = _render(_described())
    assert body.split("\n\n") == [
        "## Description: PR #7 (`0123456`, 2 commits)",
        "**Type:** Bug fix",
        "Fixes it.",
        "## Changes",
        "| File | Change |\n| :-- | :-- |\n| `a.py` | The fix. |",
        "## How to test",
        "Run pytest.",
        DESCRIPTION_TRAILER,
    ]


def test_engine_prose_cannot_notify_link_or_render():
    body = _render(
        _described(
            summary="Ping @octocat about #12 <img src=x>",
            testing="Ask @claude to review.",
        )
    )
    assert "@octocat" not in body and "&#64;octocat" in body
    assert "#12" not in body and "<img" not in body
    assert not has_mention(body, "claude")


def test_a_pipe_or_newline_cannot_break_out_of_its_cell():
    body = _render(
        _described(files=(FileChange(path="we|rd.py", change="one | two\nthree"),))
    )
    (row,) = [line for line in body.splitlines() if "rd.py" in line]
    assert row == "| `we\\|rd.py` | one \\| two three |"


def test_a_wide_pull_request_drops_rows_rather_than_the_test_plan():
    files = tuple(FileChange(path=f"f{i}.py", change="x" * 300) for i in range(1000))
    body = _render(_described(files=files))
    assert len(body) <= MAX_BODY_CHARS
    assert "## How to test" in body and body.endswith(DESCRIPTION_TRAILER)
    kept = sum(1 for line in body.splitlines() if line.startswith("| `f"))
    assert f"_And {1000 - kept} more changed files, left out for length._" in body


def test_no_file_entries_says_so():
    assert "_No changed files were described._" in _render(_described(files=()))


def test_a_moved_head_is_named():
    body = _render(_described(), moved_to="f" * 40)
    assert "no longer the head" in body and "`fffffff`" in body


def test_a_description_survives_its_json():
    description = _described()
    assert Description.from_json(description.to_json()) == description
