"""GitHubClient: a 304 is free; a 200 carries data and a fresh ETag."""

import httpx
import pytest

from pr_review_agent.poller.client import GitHubClient, GitHubClientError, RateLimit


def make_client(handler) -> GitHubClient:
    return GitHubClient(token="fake-token", transport=httpx.MockTransport(handler))


def test_first_request_sends_no_if_none_match():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(200, json=[], headers={"etag": '"abc"'})

    make_client(handler).get("/repos/o/r/pulls")
    assert seen["if_none_match"] is None


def test_conditional_get_sends_prior_etag():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(304)

    make_client(handler).get("/repos/o/r/pulls", etag='"abc"')
    assert seen["if_none_match"] == '"abc"'


def test_200_reports_changed_with_data_and_new_etag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": 1}], headers={"etag": '"v2"'})

    result = make_client(handler).get("/repos/o/r/pulls", etag='"v1"')
    assert result.changed
    assert result.data == [{"number": 1}]
    assert result.etag == '"v2"'


def test_304_reports_unchanged_and_keeps_the_etag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    result = make_client(handler).get("/repos/o/r/pulls", etag='"v1"')
    assert not result.changed
    assert result.data is None
    assert result.etag == '"v1"'


def test_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="rate limited")

    with pytest.raises(GitHubClientError, match="403"):
        make_client(handler).get("/repos/o/r/pulls")


def test_rate_limit_headers_are_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-limit": "5000"}
        return httpx.Response(200, json=[], headers=headers)

    result = make_client(handler).get("/repos/o/r/pulls")
    assert result.rate_limit == RateLimit(remaining=4999, limit=5000)


def test_missing_rate_limit_headers_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    result = make_client(handler).get("/repos/o/r/pulls")
    assert result.rate_limit is None


def test_authorization_header_is_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    make_client(handler).get("/repos/o/r/pulls")
    assert seen["auth"] == "Bearer fake-token"


def test_malformed_rate_limit_header_is_treated_as_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-ratelimit-remaining": "not-a-number", "x-ratelimit-limit": "5000"}
        return httpx.Response(200, json=[], headers=headers)

    result = make_client(handler).get("/repos/o/r/pulls")
    assert result.rate_limit is None


def test_network_error_is_wrapped_in_github_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(GitHubClientError, match="failed"):
        make_client(handler).get("/repos/o/r/pulls")


def test_plain_permission_403_is_not_retried():
    # No Retry-After header -- this is "bad token", not a rate limit -- so
    # it must fail on the first attempt, not be mistaken for one.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="bad credentials")

    with pytest.raises(GitHubClientError):
        make_client(handler).get("/repos/o/r/pulls")
    assert calls["n"] == 1


def test_secondary_rate_limit_retries_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                403, headers={"retry-after": "1"}, text="secondary rate limit"
            )
        return httpx.Response(200, json=[{"ok": True}], headers={"etag": '"v"'})

    client = GitHubClient(
        token="t",
        transport=httpx.MockTransport(handler),
        max_retries=2,
        retry_sleep=sleeps.append,
    )
    result = client.get("/repos/o/r/pulls")
    assert result.changed
    assert calls["n"] == 3
    assert sleeps == [1.0, 1.0]


def test_exhausting_retries_on_rate_limit_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"}, text="still limited")

    client = GitHubClient(
        token="t",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_sleep=lambda seconds: None,
    )
    with pytest.raises(GitHubClientError, match="429"):
        client.get("/repos/o/r/pulls")
