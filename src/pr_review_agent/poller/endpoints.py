"""The three repo-scoped GitHub REST endpoints the poller watches.

Each is repo-wide rather than per-PR: a per-PR poll would mean one request
per open PR per cycle, which does not scale and defeats the point of ETag
conditional requests. Three repo-wide endpoints at a 10 s interval is 1,080
requests/hour against a GitHub App installation budget of at least 5,000/hour
-- the rate-limit analysis in the design issue.

Every path is sorted newest-first and capped at ``per_page=100``. The sort
order is the invariant the whole design rests on: anything the poller has not
seen yet is on page 1, so the poller never paginates. A change to the sort
would silently hide new events behind a page boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from .._compat import StrEnum

#: GitHub's maximum page size. The poller reads page 1 only.
PER_PAGE = 100


class Endpoint(StrEnum):
    """Which of the three watched endpoints a poll result came from."""

    OPEN_PULLS = "open_pulls"
    ISSUE_COMMENTS = "issue_comments"  # PR conversation comments
    REVIEW_COMMENTS = "review_comments"  # inline diff comments


@dataclass(frozen=True)
class RepoEndpoints:
    """Builds the three request paths for one ``owner/name`` repository."""

    owner: str
    name: str

    def path(self, endpoint: Endpoint) -> str:
        """The request path for one watched endpoint.

        Newest-first, 100 per page: a fresh event is always on page 1, so
        one request per endpoint per cycle is a complete poll.
        """
        base = f"/repos/{self.owner}/{self.name}"
        page = f"per_page={PER_PAGE}"
        updated = f"sort=updated&direction=desc&{page}"
        return {
            Endpoint.OPEN_PULLS: (
                f"{base}/pulls?state=open&sort=created&direction=desc&{page}"
            ),
            Endpoint.ISSUE_COMMENTS: f"{base}/issues/comments?{updated}",
            Endpoint.REVIEW_COMMENTS: f"{base}/pulls/comments?{updated}",
        }[endpoint]

    def all_paths(self) -> dict[Endpoint, str]:
        """The request path for every watched endpoint."""
        return {endpoint: self.path(endpoint) for endpoint in Endpoint}
