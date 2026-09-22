"""Endpoint paths: repo-wide, matching the issue's rate-limit analysis."""

from pr_review_agent.poller.endpoints import Endpoint, RepoEndpoints
from pr_review_agent.triggers.models import CommentSource

REPO = RepoEndpoints(owner="prasadtalasila", name="pr-review-agent")

#: A short owner/name, so the publisher's paths read as paths.
ENDPOINTS = RepoEndpoints(owner="o", name="r")


def test_exactly_three_endpoints():
    assert len(REPO.all_paths()) == 3


def test_open_pulls_is_repo_scoped_not_per_pr():
    path = REPO.path(Endpoint.OPEN_PULLS)
    expected = "/repos/prasadtalasila/pr-review-agent/pulls"
    assert path.startswith(expected)
    assert "state=open" in path


def test_issue_comments_is_repo_scoped():
    path = REPO.path(Endpoint.ISSUE_COMMENTS)
    assert path.startswith("/repos/prasadtalasila/pr-review-agent/issues/comments")


def test_review_comments_is_repo_scoped():
    path = REPO.path(Endpoint.REVIEW_COMMENTS)
    assert path.startswith("/repos/prasadtalasila/pr-review-agent/pulls/comments")


def test_all_paths_are_distinct():
    paths = REPO.all_paths()
    assert len(set(paths.values())) == 3


def test_the_single_pull_request_path():
    assert REPO.pull(7) == "/repos/prasadtalasila/pr-review-agent/pulls/7"


def test_the_single_pull_request_path_is_not_watched():
    # It is read once per claimed trigger, never on the polling cycle: one
    # request per open pull request per cycle is what POLLER.md refuses.
    assert REPO.pull(7) not in REPO.all_paths().values()


# -- the publisher's paths ------------------------------------------------


def test_issue_comments_path():
    assert ENDPOINTS.issue_comments(12) == "/repos/o/r/issues/12/comments"


def test_issue_comment_path():
    assert ENDPOINTS.issue_comment(555) == "/repos/o/r/issues/comments/555"


def test_pull_request_reactions_path():
    """Where a pr_opened trigger is acknowledged: the PR has no comment."""
    assert ENDPOINTS.issue_reactions(12) == "/repos/o/r/issues/12/reactions"


def test_a_conversation_comment_takes_reactions_on_the_issues_endpoint():
    assert (
        ENDPOINTS.comment_reactions(555, CommentSource.ISSUE)
        == "/repos/o/r/issues/comments/555/reactions"
    )


def test_an_inline_comment_takes_reactions_on_the_pulls_endpoint():
    """Posted to the issues URL instead it would 404."""
    assert (
        ENDPOINTS.comment_reactions(777, CommentSource.REVIEW)
        == "/repos/o/r/pulls/comments/777/reactions"
    )
