"""The three repo-scoped GitHub REST endpoints the poller watches.

Each is repo-wide rather than per-PR: a per-PR poll would mean one request
per open PR per cycle, which does not scale and defeats the point of ETag
conditional requests. Three repo-wide endpoints at a 10 s interval is 1,080
requests/hour against a GitHub App installation budget of at least 5,000/hour
-- the rate-limit analysis in the design issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
        base = f"/repos/{self.owner}/{self.name}"
        sort_updated = "sort=updated&direction=desc"
        return {
            Endpoint.OPEN_PULLS: f"{base}/pulls?state=open&sort=created&direction=desc",
            Endpoint.ISSUE_COMMENTS: f"{base}/issues/comments?{sort_updated}",
            Endpoint.REVIEW_COMMENTS: f"{base}/pulls/comments?{sort_updated}",
        }[endpoint]

    def all_paths(self) -> dict[Endpoint, str]:
        return {endpoint: self.path(endpoint) for endpoint in Endpoint}
