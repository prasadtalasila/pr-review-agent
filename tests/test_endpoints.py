"""Endpoint paths: repo-wide, matching the issue's rate-limit analysis."""

from pr_review_agent.poller.endpoints import Endpoint, RepoEndpoints

REPO = RepoEndpoints(owner="INTO-CPS-Association", name="DTaaS")


def test_exactly_three_endpoints():
    assert len(REPO.all_paths()) == 3


def test_open_pulls_is_repo_scoped_not_per_pr():
    path = REPO.path(Endpoint.OPEN_PULLS)
    expected = "/repos/INTO-CPS-Association/DTaaS/pulls"
    assert path.startswith(expected)
    assert "state=open" in path


def test_issue_comments_is_repo_scoped():
    path = REPO.path(Endpoint.ISSUE_COMMENTS)
    assert path.startswith("/repos/INTO-CPS-Association/DTaaS/issues/comments")


def test_review_comments_is_repo_scoped():
    path = REPO.path(Endpoint.REVIEW_COMMENTS)
    assert path.startswith("/repos/INTO-CPS-Association/DTaaS/pulls/comments")


def test_all_paths_are_distinct():
    paths = REPO.all_paths()
    assert len(set(paths.values())) == 3


def test_the_single_pull_request_path():
    assert REPO.pull(7) == "/repos/INTO-CPS-Association/DTaaS/pulls/7"


def test_the_single_pull_request_path_is_not_watched():
    # It is read once per claimed trigger, never on the polling cycle: one
    # request per open pull request per cycle is what POLLER.md refuses.
    assert REPO.pull(7) not in REPO.all_paths().values()
