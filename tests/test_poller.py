"""Poller: sweeps all three endpoints and drives the adaptive interval."""

import httpx

from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import Endpoint, RepoEndpoints
from pr_review_agent.poller.poller import Poller
from pr_review_agent.store import SqliteStore

REPO = RepoEndpoints(owner="o", name="r")


def make_poller(handler) -> Poller:
    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    return Poller(client=client, endpoints=REPO)


def all_200_empty(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=[], headers={"etag": '"e1"'})


async def test_cold_start_polls_all_three_endpoints():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[], headers={"etag": '"e"'})

    await make_poller(handler).poll_once()
    assert len(seen) == 3


async def test_all_unchanged_reports_no_changed_items():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    cycle = await make_poller(handler).poll_once()
    assert not cycle.any_changed
    assert cycle.changed_items() == {}


async def test_one_changed_endpoint_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if "issues/comments" in str(request.url):
            return httpx.Response(200, json=[{"id": 1}], headers={"etag": '"e"'})
        return httpx.Response(304)

    cycle = await make_poller(handler).poll_once()
    assert cycle.any_changed
    assert list(cycle.changed_items()) == [Endpoint.ISSUE_COMMENTS]
    assert cycle.changed_items()[Endpoint.ISSUE_COMMENTS] == [{"id": 1}]


async def test_second_poll_sends_the_etag_from_the_first():
    etags_seen = []
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        etags_seen.append(request.headers.get("if-none-match"))
        call_count["n"] += 1
        return httpx.Response(200, json=[], headers={"etag": '"same"'})

    poller = make_poller(handler)
    await poller.poll_once()
    await poller.poll_once()
    # First sweep (3 requests): no prior etag. Second sweep: all three carry one.
    assert etags_seen[:3] == [None, None, None]
    assert etags_seen[3:] == ['"same"', '"same"', '"same"']


async def test_interval_snaps_to_floor_when_something_changes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 1}], headers={"etag": '"e"'})

    poller = make_poller(handler)
    poller.interval.record(changed=False)
    poller.interval.record(changed=False)
    assert poller.interval.seconds > poller.interval.min_seconds
    await poller.poll_once()
    assert poller.interval.seconds == poller.interval.min_seconds


async def test_interval_decays_when_nothing_changes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    poller = make_poller(handler)
    floor = poller.interval.seconds
    await poller.poll_once()
    assert poller.interval.seconds > floor


async def test_low_remaining_budget_forces_interval_to_ceiling():
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {
            "etag": '"e"',
            "x-ratelimit-remaining": "10",
            "x-ratelimit-limit": "5000",
        }
        return httpx.Response(200, json=[{"id": 1}], headers=headers)

    poller = make_poller(handler)
    await poller.poll_once()
    # Even though something changed (which would normally snap to the
    # floor), a near-exhausted budget takes priority.
    assert poller.interval.seconds == poller.interval.max_seconds


async def test_healthy_remaining_budget_does_not_force_ceiling():
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {
            "etag": '"e"',
            "x-ratelimit-remaining": "4999",
            "x-ratelimit-limit": "5000",
        }
        return httpx.Response(200, json=[{"id": 1}], headers=headers)

    poller = make_poller(handler)
    await poller.poll_once()
    assert poller.interval.seconds == poller.interval.min_seconds


async def test_304_with_rate_limit_headers_still_forces_the_ceiling():
    # GitHub sends the rate-limit headers on a 304 too, and a 304 costs
    # nothing -- so the budget floor must be read from them as well.
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-ratelimit-remaining": "3", "x-ratelimit-limit": "5000"}
        return httpx.Response(304, headers=headers)

    poller = make_poller(handler)
    await poller.poll_once()
    assert poller.interval.seconds == poller.interval.max_seconds


async def test_a_sqlite_store_can_back_the_etag_cache(tmp_path):
    etags_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        etags_seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, json=[], headers={"etag": '"persisted"'})

    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        await Poller(
            client=GitHubClient(token="t", transport=httpx.MockTransport(handler)),
            endpoints=REPO,
            etags=store,
        ).poll_once()
    # A restart: fresh Poller, fresh client, same file on disk.
    with SqliteStore(path) as store:
        await Poller(
            client=GitHubClient(token="t", transport=httpx.MockTransport(handler)),
            endpoints=REPO,
            etags=store,
        ).poll_once()
    assert etags_seen[:3] == [None, None, None]
    assert etags_seen[3:] == ['"persisted"'] * 3
