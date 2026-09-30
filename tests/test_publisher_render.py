"""What the comment says: the header, the sections, and the stable numbering.

Rendering is deterministic to the byte -- a vacant number means a finding
was fixed, and input order does not change the output.
"""

from dataclasses import replace

from publisher_harness import (
    HEAD,
    NOON,
    NUMBERED,
    Transport,
    body_of,
    make_publisher,
    opened,
    recorded,
    rendered,
    result_of,
)

from pr_review_agent.engine import Severity
from pr_review_agent.publisher import TRAILER, render
from pr_review_agent.report import MAX_BODY_CHARS, MAX_OMITTED_ENTRIES

# -- the rendered report -------------------------------------------------


def test_the_header_names_the_pull_request_round_commit_and_count():
    assert rendered().startswith("## Review: PR #1765 — round 3 (`deadbee`, 3 commits)")


def test_findings_are_grouped_under_their_section_headings():
    body = rendered()
    assert body.index("## Blocking") < body.index("## Should fix")
    assert body.index("## Should fix") < body.index("## Nits")


def test_a_major_finding_is_not_printed_as_blocking():
    major = (replace(NUMBERED[0], severity=Severity.MAJOR),)
    body = render(HEAD, major, pr_number=1, round_number=1, commits=1, handle="claude")
    assert "## Blocking" not in body
    assert "## Should fix" in body


def test_an_empty_section_is_omitted():
    body = render(
        HEAD, NUMBERED[:1], pr_number=1, round_number=1, commits=1, handle="claude"
    )
    assert "## Should fix" not in body
    assert "## Nits" not in body


def test_a_finding_renders_its_number_and_bold_title():
    assert (
        "2. **`script/docs.sh` copies an asset this PR deletes, "
        "so the docs build breaks.**" in rendered()
    )


def test_the_numbering_gap_left_by_a_fixed_finding_survives_rendering():
    """Items 2 and 9 -- not 1 and 2. The gaps are the information."""
    body = rendered()
    assert "2. **" in body and "9. **" in body
    assert "1. **" not in body and "3. **" not in body


def test_nits_render_as_prose_without_numbering():
    tail = rendered().split("## Nits", 1)[1]
    assert "11." not in tail
    assert "Fixed clipPath ids collide" in tail


def test_an_empty_review_still_names_the_round():
    body = render(HEAD, (), pr_number=1765, round_number=3, commits=3, handle="claude")
    assert body.startswith("## Review: PR #1765 — round 3 (`deadbee`, 3 commits)")
    assert "No issues found." in body
    assert TRAILER in body


def test_every_report_carries_the_trailer():
    assert rendered().endswith(TRAILER)


def test_the_same_findings_render_byte_identically():
    """An edit-in-place must be a no-op diff when nothing changed."""
    assert rendered() == rendered()


def test_input_order_does_not_change_the_output():
    reversed_ = render(
        HEAD,
        tuple(reversed(NUMBERED)),
        pr_number=1765,
        round_number=3,
        commits=3,
        handle="claude",
    )
    assert reversed_ == rendered()


# -- the coverage footer --------------------------------------------------


def footer_of(omitted, findings=NUMBERED):
    body = render(
        HEAD,
        findings,
        pr_number=1,
        round_number=1,
        commits=1,
        handle="claude",
        omitted=omitted,
    )
    return body.split("\n\n")[-2]


def test_the_footer_names_what_the_review_did_not_read():
    footer = footer_of((("web/node_modules/", 300), ("yarn.lock", 1)))
    assert footer == (
        "_Not reviewed: 301 changed files matched `budget.excluded_paths`: "
        "`web/node_modules/` (300 files), `yarn.lock`._"
    )


def test_nothing_withheld_renders_no_footer():
    assert rendered() == render(
        HEAD,
        NUMBERED,
        pr_number=1765,
        round_number=3,
        commits=3,
        handle="claude",
        omitted=(),
    )
    assert "Not reviewed" not in rendered()


def test_a_clean_review_still_says_what_it_did_not_read():
    """ "No issues found" over an unread lockfile is the case that matters most."""
    assert "`yarn.lock`" in footer_of((("yarn.lock", 1),), findings=())


def test_the_footer_counts_what_it_does_not_name():
    many = tuple((f"p{i}.lock", 1) for i in range(MAX_OMITTED_ENTRIES + 3))
    footer = footer_of(many)
    assert f"{MAX_OMITTED_ENTRIES + 3} changed files" in footer
    assert "and 3 more._" in footer
    assert "`p12.lock`" not in footer


def test_a_path_cannot_break_out_of_its_code_span():
    """A contributor names the file, so a backtick in it must not end the fence."""
    footer = footer_of((("a`@evil b\n## Blocking", 1),))
    assert "\n" not in footer
    assert "`` a`@evil b?## Blocking ``" in footer


def test_the_footer_counts_against_the_comment_limit():
    huge = (replace(NUMBERED[0], body="x" * MAX_BODY_CHARS),)
    body = render(
        HEAD,
        huge,
        pr_number=1,
        round_number=1,
        commits=1,
        handle="claude",
        omitted=(("yarn.lock", 1),),
    )
    assert len(body) <= MAX_BODY_CHARS
    assert "`yarn.lock`" in body


async def test_a_published_review_carries_the_recorded_footer(runs, posted):
    runs.record(
        opened(),
        head_sha=HEAD,
        result=result_of(),
        now=NOON,
        omitted=(("yarn.lock", 1),),
    )
    transport = Transport()
    run = runs.unpublished(opened().dedupe_key)
    await make_publisher(runs, posted, transport).publish(run)
    assert "`yarn.lock`._" in body_of(transport)


# -- the header's three numbers, at publish time -------------------------


async def test_the_comment_reports_the_commit_count_from_the_live_payload(runs, posted):
    transport = Transport(commits=7)
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert "7 commits" in body_of(transport)


async def test_a_payload_without_a_commit_count_still_publishes(runs, posted):
    """GitHub's field is not worth failing a publish over."""
    transport = Transport(commits=None)
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert "0 commits" in body_of(transport)


async def test_a_re_review_reports_the_next_round(runs, posted):
    first = recorded(runs, key="k1")
    await make_publisher(runs, posted, Transport()).publish(first)
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs, key="k2"))
    assert "round 2" in body_of(transport)


async def test_the_header_names_the_pull_request_being_reviewed(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert "PR #7" in body_of(transport)
