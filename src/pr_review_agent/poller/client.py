"""A GitHub REST client that polls for free.

An ETag conditional GET returns 304 with an empty body when nothing changed,
and a 304 does not count against GitHub's rate limit. Idle polling is
therefore effectively free; only a 200 (something changed) or an error costs
budget.

The client is ``asyncio``-native because the daemon is: the queue, the
per-PR leases and the budget governor are all built on top of this, and a
synchronous ``get`` would block the whole event loop for the duration of a
rate-limit backoff.

``Retry-After`` is GitHub's own signal for a transient (usually
secondary/abuse) rate limit. ``get`` retries such a response a bounded
number of times and raises anything else immediately. Two responses
deliberately fall through to the error path:

* a permanent 403 (bad token, no access), which never carries the header;
* the *primary* rate limit, which returns 403/429 with
  ``x-ratelimit-remaining: 0`` and ``x-ratelimit-reset`` but **no**
  ``Retry-After``. Sleeping to the reset can mean an hour, which is the
  poller's decision to make, not the client's -- the poller already holds
  the interval at its ceiling once ``remaining`` nears zero, so reaching
  the primary limit at all means that floor was set too low.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_MAX_RETRIES = 2
#: Longest in-process wait honoured from ``Retry-After``. GitHub may ask for
#: an hour; holding the poll loop that long is the poller's call to make, so
#: a longer cooldown is returned un-retried instead of slept through here.
MAX_RETRY_AFTER_SECONDS = 60.0
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
    #: A list for the three watched collection endpoints, and a single
    #: object for a one-off read such as ``/pulls/{n}``. Callers narrow it.
    data: list[dict] | dict | None
    etag: str | None
    rate_limit: RateLimit | None


class GitHubClient:
    """Thin wrapper over one async HTTP client, adding conditional GETs."""

    def __init__(
        self,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = httpx.AsyncClient(
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

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    async def get(self, path: str, etag: str | None = None) -> PollResult:
        """Conditionally GET ``path``, sending ``etag`` as If-None-Match."""
        headers = {"If-None-Match": etag} if etag else {}
        response = await self._send_with_retries(path, headers)
        rate_limit = RateLimit.from_headers(response.headers)
        if response.status_code == 304:
            return PollResult(
                changed=False, data=None, etag=etag, rate_limit=rate_limit
            )
        if response.status_code != 200:
            raise GitHubClientError(
                f"GET {path} -> {response.status_code}: {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise GitHubClientError(f"GET {path} returned a non-JSON body") from exc
        return PollResult(
            changed=True,
            data=data,
            etag=response.headers.get("etag"),
            rate_limit=rate_limit,
        )

    async def _send_with_retries(
        self, path: str, headers: dict[str, str]
    ) -> httpx.Response:
        """Send the GET, retrying a rate-limited response that names a
        cooldown this client is willing to wait out. Anything else -- a
        permanent error, or a cooldown longer than
        ``MAX_RETRY_AFTER_SECONDS`` -- is returned for the caller to raise
        on."""
        attempt = 0
        while True:
            try:
                response = await self._http.get(path, headers=headers)
            except httpx.HTTPError as exc:
                raise GitHubClientError(f"GET {path} failed: {exc}") from exc
            delay = retry_delay(response.headers.get("retry-after"))
            if (
                delay is None
                or response.status_code not in _RETRYABLE_STATUSES
                or attempt >= self._max_retries
            ):
                return response
            logger.warning(
                "rate limited on GET %s (status=%s), retrying in %ss",
                path,
                response.status_code,
                delay,
            )
            await self._retry_sleep(delay)
            attempt += 1


def retry_delay(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait for a ``Retry-After`` header, or ``None`` not to.

    RFC 9110 permits either a delay in seconds or an HTTP date; GitHub sends
    seconds today, but reading "come back at 07:28" as "retry in 1 s" would
    hammer the endpoint that just asked for room. A header that is
    unparseable, in the past, or asks for longer than
    ``MAX_RETRY_AFTER_SECONDS`` yields ``None``: the caller stops retrying
    and lets the poll loop schedule the next attempt instead.
    """
    if value is None:
        return None
    seconds = _seconds_from_header(value, now or datetime.now(timezone.utc))
    if seconds is None or seconds > MAX_RETRY_AFTER_SECONDS:
        return None
    return max(seconds, 0.0)


def _seconds_from_header(value: str, now: datetime) -> float | None:
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - now).total_seconds()
