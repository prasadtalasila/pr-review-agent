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
