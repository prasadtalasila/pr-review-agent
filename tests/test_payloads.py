"""Payload mapping: which raw poll items become classifier input, and which
are dropped before the classifier ever sees them."""

from datetime import datetime, timezone

from pr_review_agent.poller.payloads import comments, parse_timestamp, pull_requests

REPO = "o/r"
ALICE = {"id": 7, "login": "alice", "type": "User"}


def pr_item(**overrides) -> dict:
    item = {
        "number": 12,
        "head": {"sha": "deadbeef"},
        "user": ALICE,
        "created_at": "2026-09-17T07:11:00Z",
        "draft": False,
    }
    return {**item, **overrides}


def issue_comment(**overrides) -> dict:
    item = {
        "id": 555,
        "user": ALICE,
        "body": "@claude please look",
        "issue_url": "https://api.github.com/repos/o/r/issues/12",
        "html_url": "https://github.com/o/r/pull/12#issuecomment-555",
    }
    return {**item, **overrides}


def review_comment(**overrides) -> dict:
    item = {
        "id": 777,
        "user": ALICE,
        "body": "@claude here too",
        "pull_request_url": "https://api.github.com/repos/o/r/pulls/12",
        "commit_id": "cafebabe",
    }
    return {**item, **overrides}


def test_pull_request_is_mapped():
    (pr,) = pull_requests(REPO, [pr_item()])
    assert (pr.repo, pr.number, pr.head_sha) == ("o/r", 12, "deadbeef")
    assert pr.author.login == "alice"
    assert pr.created_at == datetime(2026, 9, 17, 7, 11, tzinfo=timezone.utc)
    assert not pr.is_draft


def test_pull_request_created_at_is_aware():
    # A naive timestamp would TypeError against the classifier's watermark.
    (pr,) = pull_requests(REPO, [pr_item()])
    assert pr.created_at.tzinfo is not None


def test_draft_flag_is_carried_through():
    (pr,) = pull_requests(REPO, [pr_item(draft=True)])
    assert pr.is_draft


def test_pull_request_with_a_ghost_author_is_skipped():
    assert list(pull_requests(REPO, [pr_item(user=None)])) == []


def test_one_unmappable_pull_request_does_not_lose_the_others():
    items = [pr_item(user=None), pr_item(number=13)]
    assert [pr.number for pr in pull_requests(REPO, items)] == [13]


def test_issue_comment_on_a_pull_request_is_mapped():
    (comment,) = comments(REPO, [issue_comment()])
    assert (comment.pr_number, comment.comment_id) == (12, 555)
    assert comment.body == "@claude please look"


def test_plain_issue_comment_is_dropped():
    # The issues endpoint returns both; reviewing issue #12 because someone
    # said @claude in it would be wrong.
    item = issue_comment(html_url="https://github.com/o/r/issues/12#issuecomment-555")
    assert list(comments(REPO, [item])) == []


def test_review_comment_is_mapped_from_its_pull_request_url():
    (comment,) = comments(REPO, [review_comment()])
    assert (comment.pr_number, comment.comment_id) == (12, 777)


def test_comment_head_sha_is_left_for_claim_time():
    # Neither payload names the pull request's head, and the review
    # comment's commit_id names the commit it was written against instead.
    assert next(iter(comments(REPO, [issue_comment()]))).head_sha is None
    assert next(iter(comments(REPO, [review_comment()]))).head_sha is None


def test_comment_with_a_ghost_author_is_skipped():
    assert list(comments(REPO, [issue_comment(user=None)])) == []


def test_comment_with_a_null_body_becomes_empty_prose():
    (comment,) = comments(REPO, [issue_comment(body=None)])
    assert comment.body == ""


def test_parse_timestamp_accepts_the_z_suffix():
    assert parse_timestamp("2026-09-17T07:11:00Z") == datetime(
        2026, 9, 17, 7, 11, tzinfo=timezone.utc
    )
