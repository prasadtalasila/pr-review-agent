"""Read one pull request, for the two things a checkout needs.

``head_sha`` is unresolved for a mention trigger -- the comment payload does
not carry one, as ``payloads.py`` explains -- and the size gate needs the
``additions`` / ``deletions`` / ``changed_files`` counts, which only the
single-pull-request endpoint reports. Both come from the same read, so
resolving the sha costs nothing beyond the request the gate already needs.

This is a per-claimed-trigger read, never a polling one: three repo-wide
endpoints at the poll interval is the budget ``POLLER.md`` protects, and one
request per open pull request per cycle is exactly what that design refuses.
"""

from __future__ import annotations

from ..triggers.models import PayloadError
from ..workspace import PullRequestFacts
from .client import GitHubClient
from .endpoints import RepoEndpoints


def pull_request_facts(payload: dict) -> PullRequestFacts:
    """Map a single-pull-request payload onto the facts a checkout needs."""
    try:
        return PullRequestFacts(
            number=int(payload["number"]),
            head_sha=str(payload["head"]["sha"]),
            base_ref=str(payload["base"]["ref"]),
            additions=int(payload["additions"]),
            deletions=int(payload["deletions"]),
            changed_files=int(payload["changed_files"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PayloadError(f"unusable pull request payload: {exc}") from exc


async def fetch_pull_request_facts(
    client: GitHubClient, endpoints: RepoEndpoints, number: int
) -> PullRequestFacts:
    """Read one pull request and map it.

    Unconditional: there is no ETag to send, because this is read once when
    a trigger is claimed rather than repeatedly on a cycle.
    """
    result = await client.get(endpoints.pull(number))
    if not isinstance(result.data, dict):
        raise PayloadError(
            f"pull request {number} returned {type(result.data).__name__}, not an object"
        )
    return pull_request_facts(result.data)
