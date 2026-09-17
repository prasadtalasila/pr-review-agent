"""A GitHub REST client that polls for free.

An ETag conditional GET returns 304 with an empty body when nothing changed,
and a 304 does not count against GitHub's rate limit. Idle polling is
therefore effectively free; only a 200 (something changed) or an error costs
budget.

A 403/429 carrying a ``Retry-After`` header is GitHub's own signal for a
transient (usually secondary/abuse) rate limit, distinct from a permanent
403 (bad token, no access) which never carries that header. ``get`` retries
the former a bounded number of times and raises the latter immediately.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_MAX_RETRIES = 2
_RETRYABLE_STATUSES = {403, 429}

logger = logging.getLogger(__name__)


class GitHubClientError(RuntimeError):
    """Raised on a non-2xx, non-304 response other than a handled rate limit,
    or when the transport itself fails (timeout, connection error, ...)."""


@dataclass(frozen=True)
class RateLimit:
    """The rate-limit headers GitHub returns on every response."""

    remaining: int
    limit: int

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> RateLimit | None:
        """Parse the rate-limit headers, or ``None`` when absent/malformed."""
        remaining, limit = (
            headers.get("x-ratelimit-remaining"),
            headers.get("x-ratelimit-limit"),
        )
        if remaining is None or limit is None:
            return None
        try:
            return cls(remaining=int(remaining), limit=int(limit))
        except ValueError:
            # Malformed header from GitHub (or a test double) -- treat as
            # "unknown" rather than crashing what is meant to be a
            # defensive parse.
            return None


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
        self,
        token: str,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_sleep: Callable[[float], None] = time.sleep,
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
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def get(self, path: str, etag: str | None = None) -> PollResult:
        """Conditionally GET ``path``, sending ``etag`` as If-None-Match."""
        headers = {"If-None-Match": etag} if etag else {}
        response = self._send_with_retries(path, headers)
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

    def _send_with_retries(self, path: str, headers: dict[str, str]) -> httpx.Response:
        """Send the GET, retrying a rate-limited response that names its own
        cooldown. A permanent error (bad token, 404, ...) never carries
        ``Retry-After`` and so is returned on the first attempt."""
        attempt = 0
        while True:
            try:
                response = self._http.get(path, headers=headers)
            except httpx.HTTPError as exc:
                raise GitHubClientError(f"GET {path} failed: {exc}") from exc
            retry_after = response.headers.get("retry-after")
            if (
                retry_after is None
                or response.status_code not in _RETRYABLE_STATUSES
                or attempt >= self._max_retries
            ):
                return response
            delay = _parse_retry_after(retry_after)
            logger.warning(
                "rate limited on GET %s (status=%s), retrying in %ss",
                path,
                response.status_code,
                delay,
            )
            self._retry_sleep(delay)
            attempt += 1


def _parse_retry_after(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 1.0
