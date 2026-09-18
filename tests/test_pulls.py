"""The single-pull-request read: head_sha for a mention, and the size numbers."""

import httpx
import pytest

from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.poller.pulls import fetch_pull_request_facts, pull_request_facts
from pr_review_agent.triggers.models import PayloadError

PAYLOAD = {
    "number": 7,
    "head": {"sha": "a" * 40},
    "base": {"ref": "main"},
    "additions": 12,
    "deletions": 3,
    "changed_files": 2,
}


def make_client(handler) -> GitHubClient:
    return GitHubClient(token="fake-token", transport=httpx.MockTransport(handler))


def test_facts_are_mapped_from_the_payload():
    facts = pull_request_facts(PAYLOAD)
    assert facts.number == 7
    assert facts.head_sha == "a" * 40
    assert facts.base_ref == "main"
    assert facts.changed_files == 2


def test_changed_lines_is_what_the_line_cap_measures():
    assert pull_request_facts(PAYLOAD).changed_lines == 15


@pytest.mark.parametrize(
    "broken",
    [
        {**PAYLOAD, "head": {}},
        {**PAYLOAD, "base": None},
        {k: v for k, v in PAYLOAD.items() if k != "changed_files"},
        {**PAYLOAD, "additions": "lots"},
    ],
)
def test_an_unusable_payload_is_a_payload_error(broken):
    with pytest.raises(PayloadError):
        pull_request_facts(broken)


async def test_the_single_pull_request_path_is_read():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=PAYLOAD, headers={"etag": '"x"'})

    facts = await fetch_pull_request_facts(
        make_client(handler), RepoEndpoints(owner="o", name="n"), 7
    )
    assert seen == ["/repos/o/n/pulls/7"]
    assert facts.head_sha == "a" * 40


async def test_head_sha_is_resolved_for_a_trigger_that_carried_none():
    """A mention's queue row has head_sha NULL; this is what fills it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=PAYLOAD, headers={"etag": '"x"'})

    facts = await fetch_pull_request_facts(
        make_client(handler), RepoEndpoints(owner="o", name="n"), 7
    )
    assert len(facts.head_sha) == 40


async def test_a_collection_response_is_refused():
    # The watched endpoints return lists; this one must not.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[PAYLOAD], headers={"etag": '"x"'})

    with pytest.raises(PayloadError, match="not an object"):
        await fetch_pull_request_facts(
            make_client(handler), RepoEndpoints(owner="o", name="n"), 7
        )
