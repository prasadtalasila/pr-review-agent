"""One poll cycle, driven end to end over payloads GitHub actually sent.

``docs/STATUS.md`` names "integration tests against recorded GitHub API
fixtures" as an acceptance criterion, and this is it. Every other test in
the suite builds its payloads from a helper holding the six fields the
mapping reads, which is what a unit test should do and what an integration
test must not: a real ``/pulls`` item carries forty of them, and the
question this file answers is whether poll, classify, enqueue and claim
still agree when the input is the real thing rather than the shape the
mapping was written against.

The fixtures under ``tests/fixtures`` were recorded from this repository by
``scripts/record_fixtures.py``, which documents the two normalisations it
applies. Nothing here touches the network: the recorded pages are replayed
through ``httpx.MockTransport``, and the conditional second cycle answers
``304`` exactly as GitHub does.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from pr_review_agent._time import parse
from pr_review_agent.budget import Governor
from pr_review_agent.comments import AgentComments
from pr_review_agent.config import Config
from pr_review_agent.daemon import COMMENTS, PULL_REQUESTS, Daemon
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.poller.interval import AdaptiveInterval
from pr_review_agent.poller.poller import Poller
from pr_review_agent.poller.pulls import fetch_pull_request_facts
from pr_review_agent.publisher import Publisher
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.runs import RunStore
from pr_review_agent.store import SqliteStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    """One recorded page, as ``json.load`` gives it back."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


PULLS: list[dict] = load("pulls_open.json")
COMMENT_PAGE: list[dict] = load("issue_comments.json")
PULL_REQUEST: dict = load("pull_request.json")

REPO = "prasadtalasila/pr-review-agent"

#: The recorded mention, and the pull request it sits on. Read out of the
#: fixture rather than restated, so a re-recording cannot leave the test
#: asserting against a comment that is no longer there.
MENTION = next(c for c in COMMENT_PAGE if c["body"].startswith("@claude"))
MENTION_PR = PULL_REQUEST["number"]
CONTRIBUTOR = MENTION["user"]["id"]
#: The author of the two pull requests the contributor did not open.
MAINTAINER = next(p["user"]["id"] for p in PULLS if p["number"] != MENTION_PR)

#: One second before the oldest thing the fixtures hold, so every recorded
#: item is new to the watermark and the cycle has something to classify.
FIRST = min(
    [parse(p["created_at"]) for p in PULLS]
    + [parse(c["updated_at"]) for c in COMMENT_PAGE]
)
BEFORE = FIRST - timedelta(seconds=1)
NOW = datetime.now(timezone.utc)


def config(*allowed: int) -> Config:
    return Config.from_mapping(
        {
            "github": {"repo": REPO},
            "triggers": {"allowlist": list(allowed), "handle": "claude"},
            "budget": {
                "session_tokens": 88_000,
                "weekly_tokens": 1_500_000,
                "max_run_tokens": 60_000,
            },
            "engine": {
                "model": "claude-sonnet-5",
                "expected_version": "2.1.274",
                "timeout_seconds": 900,
            },
        }
    )


def replay(request: httpx.Request) -> httpx.Response:
    """The recorded pages, with a real conditional answer on the second ask."""
    if request.headers.get("if-none-match"):
        return httpx.Response(304)
    url = str(request.url)
    if f"/pulls/{MENTION_PR}" in url:
        return httpx.Response(200, json=PULL_REQUEST)
    if "/pulls/comments" in url:
        body: list = []
    elif "/issues/comments" in url:
        body = COMMENT_PAGE
    else:
        body = PULLS
    return httpx.Response(200, json=body, headers={"etag": '"recorded"'})


@pytest.fixture(name="client")
def client_fixture() -> GitHubClient:
    return GitHubClient(token="t", transport=httpx.MockTransport(replay))


def make_daemon(tmp_path: Path, client: GitHubClient, config: Config) -> Daemon:
    """A daemon whose only GitHub is the recorded one."""
    store = SqliteStore(tmp_path / "state.db")
    endpoints = RepoEndpoints(config.github.owner, config.github.name)
    for stem in (PULL_REQUESTS, COMMENTS):
        store.advance_watermark(f"{stem}:{config.github.repo}", BEFORE)
    return Daemon(
        config=config,
        poller=Poller(
            client=client,
            endpoints=endpoints,
            etags=store,
            interval=AdaptiveInterval(min_seconds=0, max_seconds=0),
        ),
        store=store,
        queue=ReviewQueue(store, repo=config.github.repo),
        governor=Governor(store, config.budget),
        publisher=Publisher(
            client=client,
            endpoints=endpoints,
            runs=RunStore(store),
            posted=AgentComments(store),
            config=config.publish,
            handle=config.triggers.handle,
        ),
    )


def queued(daemon: Daemon) -> list[tuple]:
    """Every queue row, as ``(kind, pull request, actor, comment)``."""
    with daemon.store.transaction() as conn:
        return conn.execute(
            "SELECT kind, pr_number, actor_id, comment_id FROM queue "
            "ORDER BY pr_number, comment_id"
        ).fetchall()


async def test_a_recorded_listing_enqueues_the_pull_requests_it_holds(tmp_path, client):
    daemon = make_daemon(tmp_path, client, config(MAINTAINER))

    await daemon.run_once()

    opened = [
        ("pr_opened", p["number"]) for p in PULLS if p["user"]["id"] == MAINTAINER
    ]
    assert [(kind, number) for kind, number, _, _ in queued(daemon)] == opened


async def test_a_pull_request_from_outside_the_allowlist_is_not_enqueued(
    tmp_path, client
):
    """Allowlisting is on the numeric id, and the fixture holds two authors."""
    daemon = make_daemon(tmp_path, client, config(MAINTAINER))

    await daemon.run_once()

    assert not [row for row in queued(daemon) if row[2] == CONTRIBUTOR]


async def test_the_recorded_mention_enqueues_a_review_of_its_pull_request(
    tmp_path, client
):
    daemon = make_daemon(tmp_path, client, config(CONTRIBUTOR))

    await daemon.run_once()

    assert ("mention", MENTION_PR, CONTRIBUTOR, MENTION["id"]) in queued(daemon)


async def test_a_comment_on_a_plain_issue_is_never_enqueued(tmp_path, client):
    """The recorded page holds one, which is why it was recorded whole."""
    issues = {
        int(c["issue_url"].rsplit("/", 1)[-1])
        for c in COMMENT_PAGE
        if "/issues/" in c["html_url"]
    }
    daemon = make_daemon(tmp_path, client, config(MAINTAINER, CONTRIBUTOR))

    await daemon.run_once()

    assert issues and not {row[1] for row in queued(daemon)} & issues


async def test_a_second_cycle_over_unchanged_pages_enqueues_nothing_new(
    tmp_path, client
):
    """The ETag path, against the recorded pages: a 304 is not a change."""
    daemon = make_daemon(tmp_path, client, config(MAINTAINER, CONTRIBUTOR))
    await daemon.run_once()
    first = queued(daemon)

    summary = await daemon.run_once()

    assert (summary.seen, summary.enqueued) == (0, 0)
    assert queued(daemon) == first


async def test_the_claimed_trigger_resolves_its_facts_from_the_recorded_read(
    tmp_path, client
):
    """What the worker does next: one read of ``/pulls/{n}``, mapped."""
    daemon = make_daemon(tmp_path, client, config(CONTRIBUTOR))
    await daemon.run_once()

    claim = daemon.queue.claim(now=NOW, owner="w1")
    assert claim is not None
    facts = await fetch_pull_request_facts(
        client, daemon.poller.endpoints, claim.trigger.pr_number
    )

    assert facts.head_sha == PULL_REQUEST["head"]["sha"]
    assert facts.base_ref == PULL_REQUEST["base"]["ref"]
    assert facts.changed_files == PULL_REQUEST["changed_files"]
    assert (facts.state, facts.merged) == ("open", False)
