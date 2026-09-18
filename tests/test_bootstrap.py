"""Bootstrap checks: can this host actually reach what the daemon needs?"""

import sys

import httpx
import pytest

from pr_review_agent import bootstrap
from pr_review_agent._startup import TOKEN_ENV
from pr_review_agent.bootstrap import (
    CheckResult,
    check_anthropic,
    check_git,
    check_github,
    main,
)
from pr_review_agent.config import Config
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints

ENDPOINTS = RepoEndpoints("INTO-CPS-Association", "DTaaS")
HEALTHY = {"etag": '"v1"', "x-ratelimit-remaining": "4987", "x-ratelimit-limit": "5000"}


def make_client(handler) -> GitHubClient:
    return GitHubClient(token="fake-token", transport=httpx.MockTransport(handler))


def by_name(results) -> dict:
    return {result.name: result for result in results}


CONFIG_YAML = """
github:
  repo: INTO-CPS-Association/DTaaS
triggers:
  allowlist: [114395272]
budget:
  session_tokens: 88000
  weekly_tokens: 1500000
  max_run_tokens: 60000
"""


def write_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    return path


def config(tmp_path) -> Config:
    return Config.load(write_config(tmp_path))


def _canned(results):
    """Stand in for run_checks, which has already been tested on its own."""

    async def run_checks(_config, _token):
        return results

    return run_checks


def serve(first: httpx.Response, repeat: httpx.Response):
    """Answer a conditional request with ``repeat``, everything else first."""

    def handler(request: httpx.Request) -> httpx.Response:
        return repeat if "if-none-match" in request.headers else first

    return handler


async def test_a_reachable_repository_passes_every_check():
    results = await check_github(
        make_client(
            serve(httpx.Response(200, json=[], headers=HEALTHY), httpx.Response(304))
        ),
        ENDPOINTS,
    )
    assert len(results) == 4
    assert all(result.ok for result in results)


async def test_rate_limit_budget_is_reported():
    results = await check_github(
        make_client(
            serve(httpx.Response(200, json=[], headers=HEALTHY), httpx.Response(304))
        ),
        ENDPOINTS,
    )
    assert by_name(results)["github open_pulls"].detail == "4987/5000 remaining"


async def test_a_bad_token_fails_the_endpoint_checks():
    client = make_client(lambda request: httpx.Response(401, text="Bad credentials"))
    results = await check_github(client, ENDPOINTS)
    assert not any(result.ok for result in results)
    assert "401" in by_name(results)["github open_pulls"].detail


async def test_a_stripped_etag_is_caught():
    # An intercepting proxy that drops ETag turns every poll into a full 200
    # and silently burns the rate-limit budget the whole design rests on.
    client = make_client(
        serve(
            httpx.Response(200, json=[], headers=HEALTHY), httpx.Response(200, json=[])
        )
    )
    conditional = by_name(await check_github(client, ENDPOINTS))[
        "github conditional GET"
    ]
    assert conditional.ok is False
    assert "stripped" in conditional.detail


async def test_no_etag_at_all_fails_the_conditional_check():
    client = make_client(lambda request: httpx.Response(200, json=[]))
    conditional = by_name(await check_github(client, ENDPOINTS))[
        "github conditional GET"
    ]
    assert conditional.ok is False
    assert conditional.detail == "no endpoint returned an ETag"


async def test_missing_rate_limit_headers_are_reported_not_fatal():
    client = make_client(
        serve(
            httpx.Response(200, json=[], headers={"etag": '"v1"'}), httpx.Response(304)
        )
    )
    result = by_name(await check_github(client, ENDPOINTS))["github open_pulls"]
    assert result.ok is True
    assert result.detail == "no rate-limit headers"


async def test_anthropic_answering_401_counts_as_reachable():
    # No API key is sent, so 401 is the expected success case: it is a route.
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_anthropic(client)
    assert result.ok is True and "401" in result.detail


async def test_a_blocked_route_to_anthropic_fails():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("firewall said no", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        result = await check_anthropic(client)
    assert result.ok is False and "no route" in result.detail


def test_a_missing_token_is_reported_before_any_request(monkeypatch, capsys):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert main([]) == 2
    assert TOKEN_ENV in capsys.readouterr().err


def test_an_unreadable_config_is_reported(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    assert main(["--config", str(tmp_path / "absent.yaml")]) == 2
    assert "cannot read config" in capsys.readouterr().err


async def test_a_rate_limited_revisit_fails_the_conditional_check():
    def handler(request: httpx.Request) -> httpx.Response:
        if "if-none-match" in request.headers:
            return httpx.Response(403, text="Bad credentials")
        return httpx.Response(200, json=[], headers=HEALTHY)

    conditional = by_name(await check_github(make_client(handler), ENDPOINTS))[
        "github conditional GET"
    ]
    assert conditional.ok is False and "403" in conditional.detail


def _stub_git_checks(monkeypatch, results: list[CheckResult]) -> None:
    """Replace the git checks, which would otherwise reach github.com.

    Every other check here is driven through a MockTransport. `check_git`
    shells out instead, so without this the test would perform real network
    egress -- passing on a runner with internet and failing on one without,
    which is precisely the kind of test that lies.
    """

    async def canned(repo: str, base_url: str = "") -> list[CheckResult]:
        return results

    monkeypatch.setattr(bootstrap, "check_git", canned)


async def test_run_checks_covers_github_git_and_anthropic(monkeypatch, tmp_path):
    handler = serve(httpx.Response(200, json=[], headers=HEALTHY), httpx.Response(304))
    # Both clients are built before httpx.AsyncClient is patched out, since
    # GitHubClient constructs one of its own.
    github = make_client(handler)
    anthropic = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401))
    )
    monkeypatch.setattr(bootstrap, "GitHubClient", lambda token: github)
    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", lambda: anthropic)
    _stub_git_checks(
        monkeypatch,
        [
            CheckResult("git version", True, "found 2.43, need at least 2.32"),
            CheckResult("git fetch route", True, "answers"),
        ],
    )

    results = await bootstrap.run_checks(config(tmp_path), "fake-token")

    names = [result.name for result in results]
    assert names[-1] == "anthropic reachable"
    # The checkout's route is a different host from the API's, so it has to
    # be its own line in the report rather than assumed from the poller's.
    assert "git version" in names and "git fetch route" in names
    assert all(result.ok for result in results)


async def test_a_failed_git_check_fails_the_run(monkeypatch, tmp_path):
    handler = serve(httpx.Response(200, json=[], headers=HEALTHY), httpx.Response(304))
    github = make_client(handler)
    anthropic = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401))
    )
    monkeypatch.setattr(bootstrap, "GitHubClient", lambda token: github)
    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", lambda: anthropic)
    _stub_git_checks(
        monkeypatch, [CheckResult("git version", False, "found 2.25, need 2.32")]
    )

    results = await bootstrap.run_checks(config(tmp_path), "fake-token")

    assert not all(result.ok for result in results)


def test_main_reports_each_check_and_succeeds(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    monkeypatch.setattr(
        bootstrap,
        "run_checks",
        _canned([CheckResult("github open_pulls", True, "4987/5000 remaining")]),
    )
    assert main(["--config", str(write_config(tmp_path))]) == 0
    assert "PASS  github open_pulls" in capsys.readouterr().out


def test_main_exits_non_zero_when_a_check_fails(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    monkeypatch.setattr(
        bootstrap,
        "run_checks",
        _canned([CheckResult("anthropic reachable", False, "no route: blocked")]),
    )
    assert main(["--config", str(write_config(tmp_path))]) == 1
    captured = capsys.readouterr()
    assert "FAIL  anthropic reachable" in captured.out
    assert "1 check(s) failed" in captured.err


# -- the git route, which the poller's route says nothing about ----------

git_checks = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)


@git_checks
async def test_the_git_version_is_checked_first():
    """Below 2.32 GIT_CONFIG_GLOBAL is ignored without an error, so the
    checkout's hardening would be absent while appearing to be in force."""
    results = await check_git("owner/name", base_url="https://127.0.0.1:1")
    assert results[0].name == "git version"
    assert results[0].ok is True  # whatever git is installed here


@git_checks
async def test_an_unreachable_remote_is_reported_not_raised():
    # A blocked route is the commonest deployment failure, and an operator
    # needs to be told which check failed, not handed a traceback.
    results = await check_git("owner/name", base_url="https://127.0.0.1:1")
    route = next(result for result in results if result.name == "git fetch route")
    assert route.ok is False
    assert route.detail


@git_checks
async def test_a_reachable_remote_passes(git_remote, monkeypatch):
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    results = await check_git(git_remote.repo, base_url=git_remote.base_url)
    assert [result.ok for result in results] == [True, True]
