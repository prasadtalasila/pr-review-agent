"""Classifier: exactly two events may start a review."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.triggers import (
    Actor,
    Allowlist,
    Classifier,
    Comment,
    CommentSource,
    PullRequest,
    TriggerKind,
    neutralise,
)

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATER = SINCE + timedelta(hours=1)
EARLIER = SINCE - timedelta(hours=1)

ALICE = Actor(user_id=1234, login="alice")
OUTSIDER = Actor(user_id=5555, login="outsider")
BOT = Actor(user_id=7777, login="dependabot[bot]", is_bot=True)
#: The account the agent posts from. On a single-maintainer repository it
#: is also the account that opens the pull requests and types the handle,
#: which is the deployment issue #36 was raised against.
AGENT = Actor(user_id=42, login="dtaas-reviewer")


@pytest.fixture
def classifier():
    return Classifier(
        allowlist=Allowlist.from_config([ALICE.user_id]),
        since=SINCE,
    )


def make_pr(author=ALICE, created_at=LATER, is_draft=False, head_sha="abc123"):
    return PullRequest(
        repo="prasadtalasila/pr-review-agent",
        number=7,
        head_sha=head_sha,
        author=author,
        created_at=created_at,
        is_draft=is_draft,
    )


def make_comment(author=ALICE, body="@claude review", comment_id=99, updated_at=LATER):
    return Comment(
        repo="prasadtalasila/pr-review-agent",
        pr_number=7,
        comment_id=comment_id,
        author=author,
        body=body,
        updated_at=updated_at,
    )


def test_fresh_pr_from_allowlisted_author_is_accepted(classifier):
    decision = classifier.classify_pull_request(make_pr())
    assert decision.accepted
    assert decision.trigger.kind is TriggerKind.PR_OPENED
    assert decision.trigger.actor_id == ALICE.user_id


@pytest.mark.parametrize(
    ("pr", "reason"),
    [
        (make_pr(author=OUTSIDER), "author_not_allowlisted"),
        (make_pr(author=BOT), "bot_author"),
        (make_pr(is_draft=True), "draft"),
        (make_pr(created_at=EARLIER), "not_fresh"),
        (make_pr(created_at=SINCE), "not_fresh"),
    ],
)
def test_ineligible_pull_requests_are_rejected(classifier, pr, reason):
    decision = classifier.classify_pull_request(pr)
    assert not decision.accepted
    assert decision.reason == reason


def test_the_account_the_agent_posts_from_can_still_trigger_a_review():
    """Issue #36: this file used to assert the opposite, and that was the bug.

    A `self_author` / `self_commenter` check ran *before* the allowlist was
    consulted, so on a deployment where one account is both the reviewer and
    the reviewed -- which is what `config.example.yaml` describes -- the
    agent could never be triggered by the only human who used it, and the
    allowlist entry for that account was dead.

    This is the widening the change makes, stated as a test.
    """
    classifier = Classifier(
        allowlist=Allowlist.from_config([ALICE.user_id, AGENT.user_id]),
        since=SINCE,
    )
    assert classifier.classify_pull_request(make_pr(author=AGENT)).accepted
    assert classifier.classify_comment(make_comment(author=AGENT)).accepted


def test_the_allowlist_is_the_only_thing_that_was_loosened(classifier):
    """The other half of the bound: that account is gated, just not on identity.

    The fixture allowlists ALICE and nobody else, so the same two events the
    test above accepts are rejected here -- on the allowlist, which is where
    this decision now lives, and nowhere earlier.
    """
    assert classifier.classify_pull_request(make_pr(author=AGENT)).reason == (
        "author_not_allowlisted"
    )
    assert classifier.classify_comment(make_comment(author=AGENT)).reason == (
        "commenter_not_allowlisted"
    )


def test_mention_from_allowlisted_maintainer_is_accepted(classifier):
    decision = classifier.classify_comment(make_comment())
    assert decision.accepted
    assert decision.trigger.kind is TriggerKind.MENTION


def test_maintainer_can_summon_review_of_an_outsider_pr(classifier):
    # The outsider's PR is not auto-reviewed, but a maintainer may ask.
    assert not classifier.classify_pull_request(make_pr(author=OUTSIDER)).accepted
    assert classifier.classify_comment(make_comment(author=ALICE)).accepted


@pytest.mark.parametrize(
    ("comment", "reason"),
    [
        (make_comment(author=OUTSIDER), "commenter_not_allowlisted"),
        (make_comment(author=BOT), "bot_commenter"),
        (make_comment(body="looks good to me"), "no_mention"),
        (make_comment(body="```\n@claude\n```"), "no_mention"),
        (make_comment(body="> @claude review"), "no_mention"),
        (make_comment(body="use `@claude` to summon"), "no_mention"),
        (make_comment(updated_at=EARLIER), "not_fresh"),
        (make_comment(updated_at=SINCE), "not_fresh"),
    ],
)
def test_ineligible_comments_are_rejected(classifier, comment, reason):
    decision = classifier.classify_comment(comment)
    assert not decision.accepted
    assert decision.reason == reason


def test_a_comment_edited_after_the_watermark_is_accepted(classifier):
    # Editing "@claude" into an old comment bumps updated_at, and is a
    # maintainer asking for a review.
    decision = classifier.classify_comment(make_comment(updated_at=LATER))
    assert decision.accepted
    assert decision.trigger.kind is TriggerKind.MENTION


def test_a_body_the_publisher_neutralised_never_loops():
    """The loop the deleted self checks guarded, closed at the other end.

    A review body is engine prose and can contain the handle -- reviewing
    *this* repository all but guarantees it -- and the comment is edited in
    place on re-review, so it comes back fresh. What stops it summoning
    another review is that `publisher.render` ran it through `neutralise`
    first. The raw body is asserted too, because without it this test would
    pass on any body at all and prove nothing.
    """
    classifier = Classifier(
        allowlist=Allowlist.from_config([AGENT.user_id]), since=SINCE
    )
    raw = "No issues found. See @claude in triggers/mention.py."
    assert classifier.classify_comment(make_comment(author=AGENT, body=raw)).accepted
    posted = make_comment(author=AGENT, body=neutralise(raw, "claude"))
    assert classifier.classify_comment(posted).reason == "no_mention"


def test_pr_dedupe_key_is_stable_for_unchanged_head_sha(classifier):
    first = classifier.classify_pull_request(make_pr()).trigger
    again = classifier.classify_pull_request(make_pr()).trigger
    assert first.dedupe_key == again.dedupe_key


def test_pr_dedupe_key_changes_with_head_sha(classifier):
    first = classifier.classify_pull_request(make_pr(head_sha="aaa")).trigger
    second = classifier.classify_pull_request(make_pr(head_sha="bbb")).trigger
    assert first.dedupe_key != second.dedupe_key


def test_mention_dedupe_key_is_keyed_on_comment_not_head_sha(classifier):
    # A push must not re-trigger an old mention, so head_sha is excluded.
    trigger = classifier.classify_comment(make_comment(comment_id=99)).trigger
    assert trigger.head_sha is None
    assert "99" in trigger.dedupe_key


def test_distinct_comments_produce_distinct_keys(classifier):
    first = classifier.classify_comment(make_comment(comment_id=1)).trigger
    second = classifier.classify_comment(make_comment(comment_id=2)).trigger
    assert first.dedupe_key != second.dedupe_key


def test_trigger_kinds_share_no_dedupe_namespace(classifier):
    pr_key = classifier.classify_pull_request(make_pr()).trigger.dedupe_key
    mention_key = classifier.classify_comment(make_comment()).trigger.dedupe_key
    assert pr_key.split(":")[0] != mention_key.split(":")[0]


def test_accepted_decision_is_logged(classifier, caplog):
    with caplog.at_level("DEBUG", logger="pr_review_agent.triggers.classifier"):
        classifier.classify_pull_request(make_pr())
    assert any("reason=accepted" in r.message for r in caplog.records)


def test_rejected_decision_is_logged_with_its_reason(classifier, caplog):
    with caplog.at_level("DEBUG", logger="pr_review_agent.triggers.classifier"):
        classifier.classify_pull_request(make_pr(author=OUTSIDER))
    assert any("reason=author_not_allowlisted" in r.message for r in caplog.records)


def test_a_naive_watermark_is_rejected_at_construction():
    # GitHub timestamps are aware UTC; a naive `since` would TypeError on
    # the first pull request seen, which is the worst time to find out.
    with pytest.raises(ValueError, match="timezone-aware"):
        Classifier(allowlist=Allowlist.from_config([]), since=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "pr",
    [
        make_pr(author=OUTSIDER),
        make_pr(author=BOT),
        make_pr(is_draft=True),
        make_pr(created_at=EARLIER),
    ],
)
def test_every_rejection_is_logged_at_debug(classifier, caplog, pr):
    """One level for the whole record, firehose reasons and operator-relevant
    ones alike. A decision fires for every pull request on every poll, so it
    belongs on the level an operator turns on to ask "why wasn't this
    reviewed" rather than on the one they read by default."""
    with caplog.at_level("DEBUG", logger="pr_review_agent.triggers.classifier"):
        decision = classifier.classify_pull_request(pr)
    assert [r.levelname for r in caplog.records] == ["DEBUG"]
    assert decision.reason in caplog.records[0].message


def test_no_decision_is_visible_at_the_default_level(classifier, caplog):
    """The v0.12.0 failure this closes for good: roughly a hundred decisions
    per cycle at INFO, which buried everything a review actually did."""
    closed = replace(classifier, open_pull_requests=frozenset({8}))
    with caplog.at_level("INFO", logger="pr_review_agent.triggers.classifier"):
        classifier.classify_pull_request(make_pr())
        classifier.classify_pull_request(make_pr(author=OUTSIDER))
        classifier.classify_pull_request(make_pr(created_at=EARLIER))
        classifier.classify_comment(make_comment(body="looks good"))
        closed.classify_comment(make_comment())
    assert caplog.records == []


# -- comments on closed pull requests -------------------------------------
#
# `/pulls?state=open` is filtered by state; the two comment endpoints are
# repo-wide and are not. The set of open pull request numbers comes from the
# same sweep, and `None` means the sweep has not reported one yet.


def test_a_comment_on_a_closed_pull_request_is_rejected(classifier):
    closed = replace(classifier, open_pull_requests=frozenset({8}))
    decision = closed.classify_comment(make_comment())  # pr 7
    assert not decision.accepted
    assert decision.reason == "pr_not_open"


def test_a_comment_on_an_open_pull_request_is_still_accepted(classifier):
    open_ = replace(classifier, open_pull_requests=frozenset({7}))
    assert open_.classify_comment(make_comment()).accepted


def test_comments_are_not_filtered_while_the_open_set_is_unknown(classifier):
    # Failing open is deliberate: dropping every mention because the pulls
    # leg answered 304 would be worse than logging a few closed-PR lines.
    assert classifier.open_pull_requests is None
    assert classifier.classify_comment(make_comment()).accepted


# -- freshness outranks identity ------------------------------------------
#
# Anything at or below the watermark has already been decided, whoever wrote
# it, so no other reason is informative -- and `not_fresh` is the one that
# keeps a re-seen item at DEBUG.


@pytest.mark.parametrize(
    "pr",
    [
        make_pr(author=AGENT, created_at=EARLIER),
        make_pr(author=BOT, created_at=EARLIER),
        make_pr(is_draft=True, created_at=EARLIER),
        make_pr(author=OUTSIDER, created_at=EARLIER),
    ],
)
def test_a_stale_pull_request_is_rejected_not_fresh(classifier, pr):
    assert classifier.classify_pull_request(pr).reason == "not_fresh"


@pytest.mark.parametrize(
    "comment",
    [
        make_comment(author=AGENT, updated_at=EARLIER),
        make_comment(author=BOT, updated_at=EARLIER),
        make_comment(author=OUTSIDER, updated_at=EARLIER),
    ],
)
def test_a_stale_comment_is_rejected_not_fresh(classifier, comment):
    assert classifier.classify_comment(comment).reason == "not_fresh"


def test_a_mention_on_a_draft_is_still_honoured(classifier):
    # draft exists to stop unasked-for auto-review; an allowlisted human
    # typing @claude on a draft is the ask.
    assert classifier.classify_comment(make_comment()).accepted


# -- what the publisher needs to react to --------------------------------
#
# The 👀 goes on the comment somebody actually typed @claude into, so the
# trigger has to carry which comment that was and which endpoint it came
# from. A pr_opened trigger has no comment, and reacts on the pull request.


def test_a_mention_carries_the_comment_it_came_from(classifier):
    decision = classifier.classify_comment(make_comment(comment_id=4321))
    assert decision.trigger.comment_id == 4321
    assert decision.trigger.comment_source is CommentSource.ISSUE


def test_a_mention_carries_a_review_comment_source(classifier):
    comment = make_comment(comment_id=4321)
    decision = classifier.classify_comment(
        replace(comment, source=CommentSource.REVIEW)
    )
    assert decision.trigger.comment_source is CommentSource.REVIEW


def test_a_pull_request_trigger_names_no_comment(classifier):
    decision = classifier.classify_pull_request(make_pr())
    assert decision.trigger.comment_id is None
    assert decision.trigger.comment_source is None
