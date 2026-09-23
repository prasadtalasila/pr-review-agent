"""Mention parsing: only prose counts as an instruction."""

import pytest

from pr_review_agent.triggers.mention import has_mention, neutralise, strip_non_prose


@pytest.mark.parametrize(
    "body",
    [
        "@claude please review",
        "Hey @claude, take a look",
        "ping @CLAUDE",
        "@claude.",
        "(@claude)",
        "review this\n\n@claude",
    ],
)
def test_prose_mention_triggers(body):
    assert has_mention(body)


@pytest.mark.parametrize(
    "body",
    [
        "```\n@claude review this\n```",
        "```python\n# @claude\nprint('hi')\n```",
        "~~~\n@claude\n~~~",
        "````\n```\n@claude\n```\n````",
        "  ```\n  @claude\n  ```",
    ],
)
def test_fenced_code_does_not_trigger(body):
    assert not has_mention(body)


def test_unclosed_fence_swallows_rest_of_document():
    # CommonMark: an unclosed fence runs to the end of the document.
    assert not has_mention("```\n@claude review")


def test_fence_closed_then_prose_still_triggers():
    assert has_mention("```\ncode\n```\n@claude review")


@pytest.mark.parametrize(
    "body",
    [
        "> @claude please review",
        "> earlier someone said @claude\n\nI disagree",
        ">> @claude",
        "   > @claude",
    ],
)
def test_blockquote_does_not_trigger(body):
    assert not has_mention(body)


@pytest.mark.parametrize(
    "body",
    ["Type `@claude` to summon it", "``@claude`` is the handle"],
)
def test_inline_code_does_not_trigger(body):
    assert not has_mention(body)


def test_indented_code_does_not_trigger():
    assert not has_mention("Example:\n\n    @claude review\n")


@pytest.mark.parametrize(
    "body",
    [
        "mail me at user@claude.ai",
        "@claudebot review",
        "@claude-ci please",
        "claude review",
    ],
)
def test_near_misses_do_not_trigger(body):
    assert not has_mention(body)


def test_quoted_mention_with_fresh_mention_still_triggers():
    # A reply that quotes an old mention *and* adds a new one is a real request.
    assert has_mention("> @claude review\n\nAgreed — @claude go ahead")


def test_backtick_inside_fence_is_not_a_code_span():
    assert not has_mention("```\n`x` @claude\n```")


def test_blockquote_marker_inside_fence_is_literal():
    assert has_mention("```\n> code\n```\n@claude review")


@pytest.mark.parametrize(
    "body",
    [
        "@claude\n```\ncode\n```\n> quoted",
        "> quoted\n```\ncode\n```\n@claude",
        "a\n\nb\n\nc",
    ],
)
def test_strip_preserves_line_count(body):
    # split("\n") rather than splitlines(): the latter cannot observe a
    # trailing blank line, which is exactly what a blanked last line becomes.
    assert len(strip_non_prose(body).split("\n")) == len(body.split("\n"))


def test_custom_handle():
    assert has_mention("@aider review", handle="aider")
    assert not has_mention("@claude review", handle="aider")


# -- neutralise: the publisher's half of the loop -------------------------
#
# `neutralise` and `has_mention` are two halves of one rule. These tests
# assert the relationship between them rather than the escape it happens to
# use, so a change to either that is not matched in the other fails here
# rather than in production, where it is a review that pays for itself.

#: Bodies a review comment could plausibly carry. Each is a way the detector
#: or the offset arithmetic could be got wrong, not a way a reviewer writes.
NEUTRALISE_CASES = [
    "@claude",
    "@claude at the very start",
    "trailing mention @claude",
    "ping @CLAUDE about this",
    "two @claude and @claude again",
    # An unmatched backtick from engine output: this is why the escape is an
    # entity and not a pair of backticks, which this would re-pair with.
    "a ` b @claude",
    "@claude\n```\n@claude\n```\n@claude",
    "> @claude\n\n@claude",
    "    @claude\n@claude",
    # GitHub returns comment bodies with CRLF, so offsets must survive it.
    "line\r\n@claude\r\nmore",
    "nothing to do here",
]


@pytest.mark.parametrize("body", NEUTRALISE_CASES)
def test_a_neutralised_body_is_never_a_mention(body):
    assert not has_mention(neutralise(body, "claude"))


@pytest.mark.parametrize("body", NEUTRALISE_CASES)
def test_strip_preserves_every_offset(body):
    """What `neutralise` rests on: it finds a mention in the stripped text
    and edits that same index of the original."""
    assert len(strip_non_prose(body)) == len(body)


def test_neutralise_leaves_a_body_without_a_mention_alone():
    body = "No issues found.\n\n```\nemail@example.com\n```"
    assert neutralise(body, "claude") == body


def test_neutralise_does_not_reach_into_fenced_code():
    """GitHub renders no entity inside a fence, so an escape there would be
    visible to the reader -- and the detector ignores fenced code anyway."""
    body = "```\n@claude\n```"
    assert neutralise(body, "claude") == body


def test_a_neutralised_mention_still_reads_as_the_handle():
    # The reader must see no difference; only the raw body changes.
    assert neutralise("ask @claude", "claude") == "ask &#64;claude"


def test_neutralise_follows_a_custom_handle():
    assert neutralise("@aider look", "aider") == "&#64;aider look"
    assert neutralise("@claude look", "aider") == "@claude look"
