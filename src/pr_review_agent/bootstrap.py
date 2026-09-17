"""Pre-flight checks to run on the deployment host before the daemon does.

The daemon is outbound-only, so the first question about any host is whether
it can get *out* -- to `api.github.com` for polling and publishing, and to
`api.anthropic.com` for the review itself. On an allowlist-based firewall
that is the most common cause of schedule slip, and it is cheap to answer
before anything else is built on the assumption.

Three things are checked, in the order they would bite:

**Every watched endpoint answers.** The three repo-wide paths are fetched
with the same client the poller uses, so a failure here is the failure the
poller would have had: a blocked route, a bad token, a repository the token
cannot see.

**A repeat request comes back 304.** This is the one worth running even on a
host with obviously working egress. The whole rate-limit budget rests on
conditional requests being free, and an intercepting proxy that strips or
rewrites `ETag` turns every poll into a full 200 -- silently, and only
visibly once the budget runs out mid-week.

**Anthropic is reachable.** No API key is sent and none is needed: any HTTP
status proves the route exists, and only a transport error is a failure.

The token is read from the environment, never from `config.yaml`, and is
never printed -- the checks report what happened, not what was sent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from .config import Config, ConfigError
from .poller.client import GitHubClient, GitHubClientError, PollResult
from .poller.endpoints import RepoEndpoints

TOKEN_ENV = "GITHUB_TOKEN"

# Unauthenticated, so it answers 401; that is a route, which is all we ask.
ANTHROPIC_PROBE_URL = "https://api.anthropic.com/v1/models"

_CONDITIONAL = "github conditional GET"


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check, in a form an operator can act on."""

    name: str
    ok: bool
    detail: str


async def check_github(
    client: GitHubClient, endpoints: RepoEndpoints
) -> list[CheckResult]:
    """Fetch all three watched endpoints, then re-fetch one conditionally."""
    results: list[CheckResult] = []
    revisit: tuple[str, str] | None = None
    for endpoint, path in endpoints.all_paths().items():
        name = f"github {endpoint}"
        try:
            result = await client.get(path)
        except GitHubClientError as exc:
            results.append(CheckResult(name, False, str(exc)))
            continue
        results.append(CheckResult(name, True, _budget(result)))
        if revisit is None and result.etag is not None:
            revisit = (path, result.etag)
    results.append(await _check_conditional(client, revisit))
    return results


async def check_anthropic(client: httpx.AsyncClient) -> CheckResult:
    """Prove the route to Anthropic exists; any HTTP status counts."""
    name = "anthropic reachable"
    try:
        response = await client.get(ANTHROPIC_PROBE_URL)
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"no route: {exc}")
    return CheckResult(name, True, f"HTTP {response.status_code}")


async def run_checks(config: Config, token: str) -> list[CheckResult]:
    """Run every check for the repository ``config`` names."""
    endpoints = RepoEndpoints(config.github.owner, config.github.name)
    github = GitHubClient(token)
    anthropic = httpx.AsyncClient()
    try:
        results = await check_github(github, endpoints)
        results.append(await check_anthropic(anthropic))
    finally:
        await github.aclose()
        await anthropic.aclose()
    return results


async def _check_conditional(
    client: GitHubClient, revisit: tuple[str, str] | None
) -> CheckResult:
    """Re-request a path with its own ETag and insist on a 304."""
    if revisit is None:
        return CheckResult(_CONDITIONAL, False, "no endpoint returned an ETag")
    path, etag = revisit
    try:
        result = await client.get(path, etag=etag)
    except GitHubClientError as exc:
        return CheckResult(_CONDITIONAL, False, str(exc))
    if result.changed:
        return CheckResult(
            _CONDITIONAL,
            False,
            "got 200, not 304 -- an ETag is being stripped in transit, "
            "or the repository changed between the two requests",
        )
    return CheckResult(_CONDITIONAL, True, "304 Not Modified")


def _budget(result: PollResult) -> str:
    if result.rate_limit is None:
        return "no rate-limit headers"
    return f"{result.rate_limit.remaining}/{result.rate_limit.limit} remaining"


def _report(results: Sequence[CheckResult]) -> int:
    for result in results:
        print(f"{'PASS' if result.ok else 'FAIL'}  {result.name}: {result.detail}")
    failed = [result.name for result in results if not result.ok]
    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checks from the command line; non-zero exit means unusable."""
    parser = argparse.ArgumentParser(description="pr-review-agent pre-flight checks")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"{TOKEN_ENV} is not set", file=sys.stderr)
        return 2
    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return _report(asyncio.run(run_checks(config, token)))


if __name__ == "__main__":
    raise SystemExit(main())
