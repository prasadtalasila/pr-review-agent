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
from ..triggers.models import CommentSource

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

    def pull(self, number: int) -> str:
        """The path for one pull request.

        Not one of the watched endpoints, and deliberately so: this is read
        once per *claimed* trigger, to resolve ``head_sha`` and the size
        numbers the checkout gates on. Reading it on the polling cycle would
        be one request per open pull request per cycle, which is the design
        this module's docstring rejects.
        """
        return f"/repos/{self.owner}/{self.name}/pulls/{number}"

    def repository(self) -> str:
        """The repository itself, read once by the bootstrap checks.

        Never polled: it carries no event the agent reacts to. It is read to
        learn whether the token may write, which is a question worth an
        extra request exactly once, at startup, before anything has been
        reviewed and thrown away.
        """
        return f"/repos/{self.owner}/{self.name}"

    def issue_comments(self, number: int) -> str:
        """Where the publisher posts a new comment on a pull request.

        A pull request is an issue as far as this endpoint is concerned,
        which is why there is no ``/pulls/{n}/comments`` here: that one
        takes *inline* comments anchored to a diff line, and the publisher
        posts none.
        """
        return f"/repos/{self.owner}/{self.name}/issues/{number}/comments"

    def issue_comment(self, comment_id: int) -> str:
        """Where the publisher edits the comment it posted before."""
        return f"/repos/{self.owner}/{self.name}/issues/comments/{comment_id}"

    def issue_reactions(self, number: int) -> str:
        """Where a ``pr_opened`` trigger is acknowledged.

        Nobody wrote a comment to react to, so the 👀 goes on the pull
        request itself.
        """
        return f"/repos/{self.owner}/{self.name}/issues/{number}/reactions"

    def comment_reactions(self, comment_id: int, source: CommentSource) -> str:
        """Where a mention is acknowledged.

        The two comment endpoints take reactions at different URLs, and the
        ids are drawn from different sequences -- posting to the wrong one
        404s, or worse, reacts to an unrelated comment. ``source`` is
        carried from the poll payload for exactly this branch.
        """
        base = f"/repos/{self.owner}/{self.name}"
        kind = "pulls" if source is CommentSource.REVIEW else "issues"
        return f"{base}/{kind}/comments/{comment_id}/reactions"

    def all_paths(self) -> dict[Endpoint, str]:
        """The request path for every watched endpoint."""
        return {endpoint: self.path(endpoint) for endpoint in Endpoint}
