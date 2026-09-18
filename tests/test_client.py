"""GitHubClient: a 304 is free; a 200 carries data and a fresh ETag."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from pr_review_agent.poller.client import (
    GitHubClient,
    GitHubClientError,
    RateLimit,
    retry_delay,
)


def make_client(handler) -> GitHubClient:
    return GitHubClient(token="fake-token", transport=httpx.MockTransport(handler))


def record(sleeps: list):
    """An awaitable stand-in for asyncio.sleep that records its argument."""

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return sleep


async def test_first_request_sends_no_if_none_match():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(200, json=[], headers={"etag": '"abc"'})

    await make_client(handler).get("/repos/o/r/pulls")
    assert seen["if_none_match"] is None


async def test_conditional_get_sends_prior_etag():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(304)

    await make_client(handler).get("/repos/o/r/pulls", etag='"abc"')
    assert seen["if_none_match"] == '"abc"'


async def test_200_reports_changed_with_data_and_new_etag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": 1}], headers={"etag": '"v2"'})

    result = await make_client(handler).get("/repos/o/r/pulls", etag='"v1"')
    assert result.changed
    assert result.data == [{"number": 1}]
    assert result.etag == '"v2"'


async def test_304_reports_unchanged_and_keeps_the_etag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    result = await make_client(handler).get("/repos/o/r/pulls", etag='"v1"')
    assert not result.changed
    assert result.data is None
    assert result.etag == '"v1"'


async def test_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="rate limited")

    with pytest.raises(GitHubClientError, match="403"):
        await make_client(handler).get("/repos/o/r/pulls")


async def test_rate_limit_headers_are_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-limit": "5000"}
        return httpx.Response(200, json=[], headers=headers)

    result = await make_client(handler).get("/repos/o/r/pulls")
    assert result.rate_limit == RateLimit(remaining=4999, limit=5000)


async def test_missing_rate_limit_headers_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    result = await make_client(handler).get("/repos/o/r/pulls")
    assert result.rate_limit is None


async def test_authorization_header_is_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    await make_client(handler).get("/repos/o/r/pulls")
    assert seen["auth"] == "Bearer fake-token"


async def test_malformed_rate_limit_header_is_treated_as_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-ratelimit-remaining": "not-a-number", "x-ratelimit-limit": "5000"}
        return httpx.Response(200, json=[], headers=headers)

    result = await make_client(handler).get("/repos/o/r/pulls")
    assert result.rate_limit is None


async def test_network_error_is_wrapped_in_github_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(GitHubClientError, match="failed"):
        await make_client(handler).get("/repos/o/r/pulls")


async def test_plain_permission_403_is_not_retried():
    # No Retry-After header -- this is "bad token", not a rate limit -- so
    # it must fail on the first attempt, not be mistaken for one.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="bad credentials")

    with pytest.raises(GitHubClientError):
        await make_client(handler).get("/repos/o/r/pulls")
    assert calls["n"] == 1


async def test_secondary_rate_limit_retries_then_succeeds():
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
        retry_sleep=record(sleeps),
    )
    result = await client.get("/repos/o/r/pulls")
    assert result.changed
    assert calls["n"] == 3
    assert sleeps == [1.0, 1.0]


async def test_exhausting_retries_on_rate_limit_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"}, text="still limited")

    client = GitHubClient(
        token="t",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_sleep=record([]),
    )
    with pytest.raises(GitHubClientError, match="429"):
        await client.get("/repos/o/r/pulls")


async def test_retry_after_longer_than_the_cap_is_not_slept_through():
    # GitHub may ask for an hour. Blocking the poll loop that long is the
    # poller's call, not the client's, so the response is raised on instead.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "3600"}, text="slow down")

    with pytest.raises(GitHubClientError, match="429"):
        await make_client(handler).get("/repos/o/r/pulls")
    assert calls["n"] == 1


def test_http_date_retry_after_is_parsed_as_a_deadline():
    now = datetime(2026, 9, 17, 7, 28, 0, tzinfo=timezone.utc)
    assert retry_delay("Thu, 17 Sep 2026 07:28:30 GMT", now=now) == 30.0


def test_unparseable_retry_after_does_not_become_one_second():
    assert retry_delay("next tuesday") is None


def test_absent_retry_after_is_none():
    assert retry_delay(None) is None


def test_retry_after_in_the_past_is_zero_not_negative():
    now = datetime(2026, 9, 17, 8, 0, 0, tzinfo=timezone.utc)
    assert retry_delay("Thu, 17 Sep 2026 07:28:30 GMT", now=now) == 0.0


async def test_non_json_200_body_is_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(GitHubClientError, match="non-JSON"):
        await make_client(handler).get("/repos/o/r/pulls")


async def test_aclose_releases_the_connection_pool():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = make_client(handler)
    await client.get("/repos/o/r/pulls")
    await client.aclose()
    with pytest.raises(RuntimeError):
        await client.get("/repos/o/r/pulls")


# -- write verbs ---------------------------------------------------------
#
# The first requests this client makes that change something. `get` returns a
# PollResult because a conditional request has a 304 case; a write has none,
# so these return the decoded body and raise on anything but a 2xx.


async def test_post_returns_the_created_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content) == {"body": "hello"}
        return httpx.Response(201, json={"id": 9, "body": "hello"})

    assert await make_client(handler).post("/x", {"body": "hello"}) == {
        "id": 9,
        "body": "hello",
    }


async def test_patch_returns_the_updated_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        return httpx.Response(200, json={"id": 9, "body": "edited"})

    assert await make_client(handler).patch("/x", {"body": "edited"}) == {
        "id": 9,
        "body": "edited",
    }


async def test_a_200_from_post_is_accepted():
    """A duplicate reaction returns 200 rather than 201, and is not a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 9})

    assert await make_client(handler).post("/x", {}) == {"id": 9}


@pytest.mark.parametrize("status", [404, 422, 500])
async def test_a_failed_write_raises(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with pytest.raises(GitHubClientError, match=str(status)):
        await make_client(handler).post("/x", {})


async def test_a_write_retries_a_rate_limit_it_can_wait_out():
    """The same retry path `get` uses, reached through one helper."""
    sleeps: list = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
        return httpx.Response(201, json={"id": 9})

    client = GitHubClient(
        token="t", transport=httpx.MockTransport(handler), retry_sleep=record(sleeps)
    )
    assert await client.post("/x", {}) == {"id": 9}
    assert sleeps == [1.0]


async def test_a_transport_failure_on_a_write_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(GitHubClientError, match="failed"):
        await make_client(handler).post("/x", {})


async def test_a_non_json_write_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, text="<html>")

    with pytest.raises(GitHubClientError, match="non-JSON"):
        await make_client(handler).post("/x", {})


async def test_a_write_sends_no_if_none_match():
    """Conditional headers belong to polling; a write is unconditional."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(201, json={})

    await make_client(handler).post("/x", {})
    assert seen["if_none_match"] is None
