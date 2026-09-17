"""Outbound-only polling of the three GitHub REST endpoints per repository."""

from .client import GitHubClient, GitHubClientError, PollResult, RateLimit
from .endpoints import Endpoint, RepoEndpoints
from .etag_store import ETagStore
from .interval import AdaptiveInterval
from .poller import PollCycle, Poller

__all__ = [
    "AdaptiveInterval",
    "ETagStore",
    "Endpoint",
    "GitHubClient",
    "GitHubClientError",
    "PollCycle",
    "PollResult",
    "Poller",
    "RateLimit",
    "RepoEndpoints",
]
