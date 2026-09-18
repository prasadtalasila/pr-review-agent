"""Turn raw poll payloads into the models the classifier consumes.

This is the seam the classifier was written against: everything above it is
pure functions over dataclasses, and every quirk of the GitHub REST shape is
resolved here.

Three of those quirks decide the design.

**A comment payload does not name its pull request.** ``/issues/comments``
carries an ``issue_url``; ``/pulls/comments`` carries a ``pull_request_url``.
The number is the last path segment of whichever is present, so it costs no
extra request.

**A comment payload does not distinguish a pull request from an issue.** The
issues endpoint returns both, and reviewing issue #7 because someone said
``@claude`` in it would be wrong. A comment on a pull request has an
``html_url`` under ``/pull/``; a plain issue comment does not. That field is
already in the payload, so the filter is free.

**A comment payload carries no head SHA.** ``head_sha`` is therefore left
unresolved here and read when the trigger is claimed, by ``pulls.py`` --
which needs the same request for the checkout's size gate, so resolving the
sha costs nothing extra. Resolving it *here* would cost one request per
comment on every poll to learn a value that can go stale before the worker
starts -- and the worker already re-reads it immediately before posting, so
the second read is the only one that counts.
Review comments do carry a ``commit_id``, but it names the commit the
comment was written against rather than the pull request's head, so it is
not used either.

An item that cannot be mapped -- a ghost author, a missing field -- is
skipped with a warning rather than failing the cycle: one malformed item
must not stop the other ninety-nine from being classified.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

from ..triggers.models import Actor, Comment, PayloadError, PullRequest

logger = logging.getLogger(__name__)


def parse_timestamp(value: str) -> datetime:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime.

    GitHub writes the ``Z`` suffix, which ``datetime.fromisoformat`` does not
    accept before Python 3.11.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def pull_requests(repo: str, items: Iterable[dict]) -> Iterator[PullRequest]:
    """Map the ``/pulls?state=open`` payload, skipping unusable items."""
    for item in items:
        try:
            yield PullRequest(
                repo=repo,
                number=int(item["number"]),
                head_sha=item["head"]["sha"],
                author=Actor.from_api(item.get("user")),
                created_at=parse_timestamp(item["created_at"]),
                is_draft=bool(item.get("draft", False)),
            )
        except (PayloadError, KeyError, TypeError, ValueError) as exc:
            _skip("pull request", item, exc)


def comments(repo: str, items: Iterable[dict]) -> Iterator[Comment]:
    """Map either comments payload, dropping anything not on a pull request.

    Both endpoints are handled by one function because the classifier treats
    a conversation comment and an inline diff comment identically: the only
    difference in the payloads is which URL field names the pull request.
    """
    for item in items:
        try:
            number = _pr_number(item)
            if number is None:
                continue
            yield Comment(
                repo=repo,
                pr_number=number,
                comment_id=int(item["id"]),
                author=Actor.from_api(item.get("user")),
                body=item.get("body") or "",
                updated_at=parse_timestamp(item["updated_at"]),
            )
        except (PayloadError, AttributeError, KeyError, TypeError, ValueError) as exc:
            _skip("comment", item, exc)


def _pr_number(item: dict) -> int | None:
    """The pull request a comment belongs to, or ``None`` if it is not one."""
    url = item.get("pull_request_url")
    if url is None:
        if "/pull/" not in (item.get("html_url") or ""):
            return None  # a plain issue comment
        url = item.get("issue_url")
    if not isinstance(url, str):
        return None
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def _skip(kind: str, item: dict, exc: Exception) -> None:
    logger.warning("skipping unmappable %s id=%s: %s", kind, item.get("id"), exc)
