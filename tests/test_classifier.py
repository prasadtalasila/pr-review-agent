"""Classifier: exactly two events may start a review."""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.triggers import (
    Actor,
    Allowlist,
    Classifier,
    Comment,
    PullRequest,
    TriggerKind,
)

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATER = SINCE + timedelta(hours=1)
EARLIER = SINCE - timedelta(hours=1)

ALICE = Actor(user_id=1234, login="alice")
OUTSIDER = Actor(user_id=5555, login="outsider")
BOT = Actor(user_id=7777, login="dependabot[bot]", is_bot=True)
AGENT = Actor(user_id=42, login="dtaas-reviewer")


@pytest.fixture
def classifier():
    return Classifier(
        allowlist=Allowlist.from_config([ALICE.user_id]),
        since=SINCE,
        agent_user_id=AGENT.user_id,
    )


def make_pr(author=ALICE, created_at=LATER, is_draft=False, head_sha="abc123"):
    return PullRequest(
        repo="INTO-CPS-Association/DTaaS",
        number=7,
        head_sha=head_sha,
        author=author,
        created_at=created_at,
        is_draft=is_draft,
    )


def make_comment(author=ALICE, body="@claude review", comment_id=99, updated_at=LATER):
    return Comment(
        repo="INTO-CPS-Association/DTaaS",
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
        (make_pr(author=AGENT), "self_author"),
        (make_pr(is_draft=True), "draft"),
        (make_pr(created_at=EARLIER), "not_fresh"),
        (make_pr(created_at=SINCE), "not_fresh"),
    ],
)
def test_ineligible_pull_requests_are_rejected(classifier, pr, reason):
    decision = classifier.classify_pull_request(pr)
    assert not decision.accepted
    assert decision.reason == reason


def test_an_allowlisted_agent_still_cannot_trigger_itself():
    """The shipped config lists the agent's own id, so this ordering matters.

    `config.example.yaml` puts the reviewer account in the allowlist as belt
    and braces. That is only harmless because the self checks run *before*
    the allowlist is consulted -- reverse them and the agent's own review
    comment would summon another review, indefinitely.
    """
    classifier = Classifier(
        allowlist=Allowlist.from_config([ALICE.user_id, AGENT.user_id]),
        since=SINCE,
        agent_user_id=AGENT.user_id,
    )
    assert classifier.classify_pull_request(make_pr(author=AGENT)).reason == (
        "self_author"
    )
    assert classifier.classify_comment(make_comment(author=AGENT)).reason == (
        "self_commenter"
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
        (make_comment(author=AGENT), "self_commenter"),
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


def test_agents_own_comment_never_loops(classifier):
    # The agent posts "no issues found" — that must not re-trigger it.
    own = make_comment(author=AGENT, body="No issues found for @claude review")
    assert not classifier.classify_comment(own).accepted


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


def test_accepted_decision_is_logged_at_info(classifier, caplog):
    with caplog.at_level("INFO", logger="pr_review_agent.triggers.classifier"):
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
        make_pr(author=AGENT),
        make_pr(is_draft=True),
    ],
)
def test_operator_relevant_rejections_are_visible_at_info(classifier, caplog, pr):
    # At the default level these are the whole answer to "why wasn't this
    # reviewed?"; at DEBUG the log exists but nobody sees it.
    with caplog.at_level("INFO", logger="pr_review_agent.triggers.classifier"):
        decision = classifier.classify_pull_request(pr)
    assert any(decision.reason in r.message for r in caplog.records)


def test_the_two_firehose_reasons_stay_at_debug(classifier, caplog):
    # not_fresh fires once per already-open PR and no_mention once per
    # comment; at INFO they would bury everything above.
    with caplog.at_level("INFO", logger="pr_review_agent.triggers.classifier"):
        classifier.classify_pull_request(make_pr(created_at=EARLIER))
        classifier.classify_comment(make_comment(body="looks good"))
    assert caplog.records == []


def test_a_mention_on_a_draft_is_still_honoured(classifier):
    # draft exists to stop unasked-for auto-review; an allowlisted human
    # typing @claude on a draft is the ask.
    assert classifier.classify_comment(make_comment()).accepted
