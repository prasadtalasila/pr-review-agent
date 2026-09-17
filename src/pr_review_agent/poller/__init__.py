"""Outbound-only polling of the three GitHub REST endpoints per repository."""

from .client import GitHubClient, GitHubClientError, PollResult, RateLimit
from .endpoints import Endpoint, RepoEndpoints
from .etag_store import ETagCache, ETagStore
from .interval import AdaptiveInterval
from .payloads import comments, parse_timestamp, pull_requests
from .poller import PollCycle, Poller

__all__ = [
    "AdaptiveInterval",
    "ETagCache",
    "ETagStore",
    "Endpoint",
    "GitHubClient",
    "GitHubClientError",
    "PollCycle",
    "PollResult",
    "Poller",
    "RateLimit",
    "RepoEndpoints",
    "comments",
    "parse_timestamp",
    "pull_requests",
]
