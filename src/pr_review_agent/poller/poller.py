"""Poll one repository's three endpoints and report what changed.

One poll cycle is a full sweep of all three endpoints. The interval is
adjusted once at the end of the cycle, from whether *any* endpoint changed --
an active PR-comments endpoint should keep the whole repo polling fast even
if open-PRs itself is quiet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import GitHubClient
from .endpoints import Endpoint, RepoEndpoints
from .etag_store import ETagStore
from .interval import AdaptiveInterval


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
    etags: ETagStore = field(default_factory=ETagStore)
    interval: AdaptiveInterval = field(default_factory=AdaptiveInterval)

    def poll_once(self) -> PollCycle:
        """Sweep all three endpoints once and update the poll interval."""
        results: dict[Endpoint, list[dict] | None] = {}
        any_changed = False
        for endpoint, path in self.endpoints.all_paths().items():
            result = self.client.get(path, etag=self.etags.get(path))
            self.etags.set(path, result.etag)
            results[endpoint] = result.data if result.changed else None
            any_changed = any_changed or result.changed
        self.interval.record(changed=any_changed)
        return PollCycle(results=results, any_changed=any_changed)
