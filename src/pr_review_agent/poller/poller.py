"""Poll one repository's three endpoints and report what changed.

One poll cycle is a full sweep of all three endpoints. The interval is
adjusted once at the end of the cycle, from whether *any* endpoint changed --
an active PR-comments endpoint should keep the whole repo polling fast even
if open-PRs itself is quiet.

If any endpoint reports the rate-limit budget nearly exhausted, the interval
is forced to its ceiling instead -- an active repo is not worth chasing at
the cost of running out of requests before the window resets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .client import GitHubClient
from .endpoints import Endpoint, RepoEndpoints
from .etag_store import ETagCache, ETagStore
from .interval import AdaptiveInterval

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_FLOOR = 50


@dataclass
class PollCycle:
    """The results of one sweep across all three endpoints."""

    results: dict[Endpoint, list[dict] | None]
    any_changed: bool

    def changed_items(self) -> dict[Endpoint, list[dict]]:
        """The endpoints that changed, each with its fresh payload."""
        return {ep: data for ep, data in self.results.items() if data is not None}


@dataclass
class Poller:
    """Owns the ETag cache and adaptive interval for one repository."""

    client: GitHubClient
    endpoints: RepoEndpoints
    etags: ETagCache = field(default_factory=ETagStore)
    interval: AdaptiveInterval = field(default_factory=AdaptiveInterval)
    rate_limit_floor: int = DEFAULT_RATE_LIMIT_FLOOR

    async def poll_once(self) -> PollCycle:
        """Sweep all three endpoints once and update the poll interval."""
        results: dict[Endpoint, list[dict] | None] = {}
        any_changed = False
        lowest_remaining: int | None = None
        for endpoint, path in self.endpoints.all_paths().items():
            result = await self.client.get(path, etag=self.etags.get(path))
            self.etags.set(path, result.etag)
            # The three watched endpoints are collections, so anything else
            # is a shape change at GitHub's end rather than a poll result.
            items = result.data if isinstance(result.data, list) else None
            results[endpoint] = items if result.changed else None
            any_changed = any_changed or result.changed
            if result.rate_limit is not None:
                remaining = result.rate_limit.remaining
                lowest_remaining = (
                    remaining
                    if lowest_remaining is None
                    else min(lowest_remaining, remaining)
                )
        self._update_interval(any_changed, lowest_remaining)
        repo = f"{self.endpoints.owner}/{self.endpoints.name}"
        logger.debug(
            "poll cycle repo=%s changed=%s interval=%ss remaining=%s",
            repo,
            any_changed,
            self.interval.seconds,
            lowest_remaining,
        )
        return PollCycle(results=results, any_changed=any_changed)

    def _update_interval(self, any_changed: bool, lowest_remaining: int | None) -> None:
        if lowest_remaining is not None and lowest_remaining <= self.rate_limit_floor:
            logger.warning(
                "rate-limit budget nearly exhausted (remaining=%s, floor=%s); "
                "holding interval at the ceiling",
                lowest_remaining,
                self.rate_limit_floor,
            )
            self.interval.force_ceiling()
        else:
            self.interval.record(changed=any_changed)
