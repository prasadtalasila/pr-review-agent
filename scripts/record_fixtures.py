"""Re-record the GitHub payloads the integration test replays.

The suite's other GitHub payloads are hand-written dicts holding the six
fields the mapping reads, which is what a unit test wants and what an
integration test must not settle for: a real ``/pulls`` item carries forty
fields, and the shapes that broke this agent in production -- a comment
whose ``html_url`` is an issue's, a pull request that closed while it sat in
the queue -- are shapes no hand-written dict thinks to include.

Everything recorded here is public, read with no authentication. Two
normalisations are applied, and they are the only edits:

* the pull requests are recorded ``open``, because the endpoint the daemon
  polls is ``state=open`` and this repository has no pull request open for
  long; and
* one recorded comment's body gains an ``@claude`` mention, because nobody
  has ever addressed this agent in the repository that hosts it.

Usage: ``python scripts/record_fixtures.py [owner/repo]``.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: The pull request the recorded mention sits on, and the oldest three the
#: listing holds: small numbers, so the fixtures stay legible.
MENTION_PR = 2
LISTING = (2, 6, 7)

MENTION = "@claude review this"


def _get(repo: str, path: str) -> list | dict:
    """One unauthenticated read of the public REST API."""
    request = urllib.request.Request(
        f"{API}/{repo}/{path}", headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _opened(item: dict) -> dict:
    """A recorded pull request as the ``state=open`` endpoint would carry it.

    ``merged`` goes with ``state``: a pull request recorded open and merged
    is a shape GitHub never serves, and the worker reads both.
    """
    reopened = {**item, "state": "open", "closed_at": None, "merged_at": None}
    for key, value in (("merged", False), ("merged_by", None)):
        if key in reopened:
            reopened[key] = value
    return reopened


def record_pulls(repo: str) -> list[dict]:
    """The listing page, sliced to ``LISTING`` and recorded open."""
    page = _get(repo, "pulls?state=all&direction=asc&per_page=100")
    assert isinstance(page, list)
    by_number = {item["number"]: item for item in page}
    return [_opened(by_number[number]) for number in LISTING]


def record_comments(repo: str) -> list[dict]:
    """A comments page: two on a pull request, one on a plain issue.

    The plain issue comment is the half of this fixture that is easy to
    forget and expensive to get wrong -- it is what the classifier must
    drop, and a page that held only pull request comments would pass
    whether the filter worked or not.
    """
    page = _get(repo, "issues/comments?per_page=100")
    assert isinstance(page, list)
    on_pull = [c for c in page if f"/pull/{MENTION_PR}#" in c["html_url"]]
    on_issue = [c for c in page if "/issues/" in c["html_url"]]
    mention = {**on_pull[0], "body": f"{MENTION}\n\n{on_pull[0]['body']}"}
    return sorted([mention, on_pull[1], on_issue[0]], key=lambda c: c["updated_at"])


def record_pull_request(repo: str) -> dict:
    """The single-pull-request read a claimed trigger makes."""
    item = _get(repo, f"pulls/{MENTION_PR}")
    assert isinstance(item, dict)
    return _opened(item)


def main(repo: str) -> None:
    written = {
        "pulls_open.json": record_pulls(repo),
        "issue_comments.json": record_comments(repo),
        "pull_request.json": record_pull_request(repo),
    }
    for name, payload in written.items():
        path = FIXTURES / name
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(FIXTURES.parent.parent)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "prasadtalasila/pr-review-agent")
