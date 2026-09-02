"""A GitHub REST client that polls for free.

An ETag conditional GET returns 304 with an empty body when nothing changed,
and a 304 does not count against GitHub's rate limit. Idle polling is
therefore effectively free; only a 200 (something changed) or an error costs
budget.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

GITHUB_API_BASE = "https://api.github.com"


class GitHubClientError(RuntimeError):
    """Raised on a non-2xx, non-304 response other than a handled rate limit."""


@dataclass(frozen=True)
class RateLimit:
    """The rate-limit headers GitHub returns on every response."""

    remaining: int
    limit: int

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> RateLimit | None:
        remaining, limit = (
            headers.get("x-ratelimit-remaining"),
            headers.get("x-ratelimit-limit"),
        )
        if remaining is None or limit is None:
            return None
        return cls(remaining=int(remaining), limit=int(limit))


@dataclass(frozen=True)
class PollResult:
    """The outcome of one conditional GET against one endpoint."""

    changed: bool
    data: list[dict] | None
    etag: str | None
    rate_limit: RateLimit | None


class GitHubClient:
    """Thin wrapper over one HTTP client, adding conditional GET semantics."""

    def __init__(
        self, token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = httpx.Client(
            base_url=GITHUB_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, etag: str | None = None) -> PollResult:
        """Conditionally GET ``path``, sending ``etag`` as If-None-Match."""
        headers = {"If-None-Match": etag} if etag else {}
        response = self._http.get(path, headers=headers)
        rate_limit = RateLimit.from_headers(response.headers)
        if response.status_code == 304:
            return PollResult(
                changed=False, data=None, etag=etag, rate_limit=rate_limit
            )
        if response.status_code != 200:
            raise GitHubClientError(
                f"GET {path} -> {response.status_code}: {response.text[:200]}"
            )
        return PollResult(
            changed=True,
            data=response.json(),
            etag=response.headers.get("etag"),
            rate_limit=rate_limit,
        )
