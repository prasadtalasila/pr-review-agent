"""Read one pull request, for the two things a checkout needs.

``head_sha`` is unresolved for a mention trigger -- the comment payload does
not carry one, as ``payloads.py`` explains -- and the size gate needs the
``additions`` / ``deletions`` / ``changed_files`` counts, which only the
single-pull-request endpoint reports. Both come from the same read, so
resolving the sha costs nothing beyond the request the gate already needs.

The same read also says whether the pull request is still open, which is
what lets the worker refuse a review nobody will read before it has paid
for one. That fact is free here and unavailable anywhere else: the queue
row was written when the pull request still was open.

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
    """Map a single-pull-request payload onto the facts a checkout needs.

    ``state`` is required rather than defaulted, and that is the one choice
    here worth defending. It is read to refuse a review of a pull request
    nobody can act on any more, so a default of ``"open"`` would turn any
    change in the payload's shape into the guard quietly switching itself
    off -- which is the failure this mapping exists to prevent. Missing is
    therefore an unusable payload, exactly as a missing ``head`` is.

    ``merged`` may default: GitHub omits it nowhere the agent reads, and a
    pull request that was merged is already ``closed`` by ``state``, so a
    missing flag cannot be the only thing standing between a spend and a
    refusal.
    """
    try:
        return PullRequestFacts(
            number=int(payload["number"]),
            head_sha=str(payload["head"]["sha"]),
            base_ref=str(payload["base"]["ref"]),
            additions=int(payload["additions"]),
            deletions=int(payload["deletions"]),
            changed_files=int(payload["changed_files"]),
            state=str(payload["state"]),
            merged=bool(payload.get("merged", False)),
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
        shape = type(result.data).__name__
        raise PayloadError(f"pull request {number} returned {shape}, not an object")
    return pull_request_facts(result.data)
