"""The verb after the handle: ``@claude describe`` and ``@claude review``.

A closed set of two. Anything else a mention says -- no word at all, an
unknown one, prose -- is a review, which is what ``@claude`` meant before
verbs existed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.triggers import (
    Actor,
    Allowlist,
    Classifier,
    Command,
    Comment,
    PullRequest,
    mention_verb,
)

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
ALICE = Actor(user_id=1234, login="alice")


def _classifier() -> Classifier:
    return Classifier(allowlist=Allowlist.from_config([ALICE.user_id]), since=SINCE)


def _comment(body: str) -> Comment:
    return Comment(
        repo="o/r",
        pr_number=7,
        comment_id=99,
        author=ALICE,
        body=body,
        updated_at=SINCE + timedelta(hours=1),
    )


@pytest.mark.parametrize(
    ("body", "command"),
    [
        ("@claude describe", Command.DESCRIBE),
        ("@Claude DESCRIBE this please", Command.DESCRIBE),
        ("@claude describe.", Command.DESCRIBE),
        ("thanks!\r\n\r\n@claude describe\r\n", Command.DESCRIBE),
        ("@claude review", Command.REVIEW),
        ("@claude", Command.REVIEW),
        ("@claude please take a look", Command.REVIEW),
        ("@claude improve", Command.REVIEW),
        # The verb has to be the word after the handle, on its line.
        ("@claude\ndescribe", Command.REVIEW),
        ("@claude, describe", Command.REVIEW),
        ("please describe this @claude", Command.REVIEW),
        # Bounded like the handle, so a longer word is not the verb.
        ("@claude describe-it", Command.REVIEW),
        ("@claude describes", Command.REVIEW),
        # Only the first mention in prose is read.
        ("@claude review, and @claude describe", Command.REVIEW),
        ("`@claude describe` is the syntax. @claude", Command.REVIEW),
        ("> @claude describe\n\n@claude", Command.REVIEW),
    ],
)
def test_the_verb_chooses_the_command(body: str, command: Command) -> None:
    decision = _classifier().classify_comment(_comment(body))
    assert decision.trigger is not None
    assert decision.trigger.command is command


def test_a_quoted_describe_is_not_a_mention_at_all() -> None:
    decision = _classifier().classify_comment(_comment("`@claude describe`"))
    assert decision.trigger is None
    assert decision.reason == "no_mention"


def test_a_describe_request_still_needs_the_allowlist() -> None:
    outsider = Actor(user_id=5555, login="alice-renamed")
    comment = _comment("@claude describe")
    comment = Comment(**{**comment.__dict__, "author": outsider})
    assert _classifier().classify_comment(comment).reason == (
        "commenter_not_allowlisted"
    )


def test_a_fresh_pull_request_is_always_a_review() -> None:
    pr = PullRequest(
        repo="o/r",
        number=7,
        head_sha="abc",
        author=ALICE,
        created_at=SINCE + timedelta(hours=1),
    )
    trigger = _classifier().classify_pull_request(pr).trigger
    assert trigger is not None and trigger.command is Command.REVIEW


def test_the_verb_is_read_after_the_configured_handle() -> None:
    assert mention_verb("@reviewer describe", "reviewer") == "describe"
    assert mention_verb("@claude describe", "reviewer") == ""
    assert mention_verb("no mention here", "claude") == ""
