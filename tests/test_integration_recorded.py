"""One poll cycle against payloads recorded from the real GitHub API.

`docs/STATUS.md` names "integration tests against recorded GitHub API
fixtures" as an acceptance criterion, and until this module there were none.
Everything else in the suite feeds the daemon dictionaries written by hand,
which prove the code reads the fields the *test author* remembered; they
cannot catch a field GitHub sends in a shape nobody anticipated, because the
test and the code were written from the same assumption.

What runs here is the whole cycle -- ``Daemon.run_once``, so the poller, the
ETag store, the payload mapping, the classifier, the watermarks and the queue
-- against `tests/fixtures/github/*.json`, through ``httpx.MockTransport``. No
network, no tokens, no engine.

The fixtures were recorded from `prasadtalasila/pr-review-agent` itself on
2026-09-26 (see `tests/fixtures/github/README.md` for exactly what was edited
and why). Their value is in the two dozen fields each object carries that the
hand-written payloads elsewhere do not, and in the mix the comments page
happens to hold: comments on plain issues, a comment on a closed pull request,
and a mention on an open one -- the three outcomes the comment filter has to
tell apart, none of them arranged by the test.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from pr_review_agent.budget import Governor
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

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"

REPO = "prasadtalasila/pr-review-agent"
#: The numeric id of the account that wrote everything recorded here.
AUTHOR_ID = 9206466

#: Before the oldest recorded timestamp, so every item is fresh. Set as the
#: stored watermark rather than seeded, which is what a daemon that has run
#: before looks like -- the cold-start path is covered in test_daemon.py.
SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)

CONFIG = Config.from_mapping(
    {
        "github": {"repo": REPO},
        "triggers": {"allowlist": [AUTHOR_ID], "handle": "claude"},
        "budget": {
            "session_tokens": 88_000,
            "weekly_tokens": 1_500_000,
            "max_run_tokens": 60_000,
        },
        # Required, and never reached: nothing here claims a row, so no
        # engine is ever constructed and no token is ever spent.
        "engine": {
            "model": "claude-sonnet-5",
            "expected_version": "2.1.274",
            "timeout_seconds": 900,
        },
    }
)

PULLS_WM = f"{PULL_REQUESTS}:{REPO}"
COMMENTS_WM = f"{COMMENTS}:{REPO}"


def recorded(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


OPEN_PULLS = recorded("pulls_open")
ISSUE_COMMENTS = recorded("issue_comments")
REVIEW_COMMENTS = recorded("review_comments")
PULL = recorded("pull")


class Recording:
    """Serves the recorded pages, then `304`s the way GitHub does.

    The second cycle is the point of the counter: a daemon that polls again
    with the ETags it stored must be told *unchanged*, and must enqueue
    nothing rather than re-enqueueing everything it already has.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if "/pulls/comments" in url:
            body, tag = REVIEW_COMMENTS, '"review"'
        elif "/issues/comments" in url:
            body, tag = ISSUE_COMMENTS, '"issue"'
        else:
            body, tag = OPEN_PULLS, '"pulls"'
        if request.headers.get("if-none-match") == tag:
            return httpx.Response(304, headers={"etag": tag})
        return httpx.Response(200, json=body, headers={"etag": tag})


def make_daemon(tmp_path, handler) -> Daemon:
    store = SqliteStore(tmp_path / "state.db")
    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    endpoints = RepoEndpoints(CONFIG.github.owner, CONFIG.github.name)
    return Daemon(
        config=CONFIG,
        poller=Poller(
            client=client,
            endpoints=endpoints,
            etags=store,
            interval=AdaptiveInterval(min_seconds=0, max_seconds=0),
        ),
        store=store,
        queue=ReviewQueue(store, repo=REPO),
        governor=Governor(store, CONFIG.budget),
        publisher=Publisher(
            client=client,
            endpoints=endpoints,
            runs=RunStore(store),
            config=CONFIG.publish,
            handle=CONFIG.triggers.handle,
        ),
    )


def prepared(tmp_path, handler) -> Daemon:
    daemon = make_daemon(tmp_path, handler)
    daemon.store.advance_watermark(PULLS_WM, SINCE)
    daemon.store.advance_watermark(COMMENTS_WM, SINCE)
    return daemon


def rows(daemon: Daemon) -> list[dict]:
    with daemon.store.transaction() as conn:
        conn.row_factory = None
        cursor = conn.execute(
            "SELECT kind, pr_number, dedupe_key, comment_id FROM queue "
            "ORDER BY pr_number, kind"
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


# -- what the recorded cycle produces ------------------------------------


def test_the_recorded_fixtures_are_what_this_module_assumes():
    """Guards every test below against a re-recording that changed the mix."""
    assert [pull["number"] for pull in OPEN_PULLS] == [95, 94]
    assert not any(pull["draft"] for pull in OPEN_PULLS)
    assert len(ISSUE_COMMENTS) == 5
    assert sum("/pull/" in c["html_url"] for c in ISSUE_COMMENTS) == 2
    assert REVIEW_COMMENTS == []


async def test_one_cycle_enqueues_both_open_pull_requests_and_the_mention(tmp_path):
    daemon = prepared(tmp_path, Recording())

    summary = await daemon.run_once()

    assert summary.enqueued == 3
    assert rows(daemon) == [
        {
            "kind": "pr_opened",
            "pr_number": 94,
            "dedupe_key": f"pr_opened:{REPO}:94:{OPEN_PULLS[1]['head']['sha']}",
            "comment_id": None,
        },
        {
            "kind": "mention",
            "pr_number": 95,
            "dedupe_key": f"mention:{REPO}:95:{ISSUE_COMMENTS[4]['id']}",
            "comment_id": ISSUE_COMMENTS[4]["id"],
        },
        {
            "kind": "pr_opened",
            "pr_number": 95,
            "dedupe_key": f"pr_opened:{REPO}:95:{OPEN_PULLS[0]['head']['sha']}",
            "comment_id": None,
        },
    ]


async def test_a_comment_on_a_plain_issue_is_not_a_pull_request_at_all(tmp_path):
    """Three of the five recorded comments are on issues, not pull requests.

    They are dropped in the payload mapping, before the classifier sees them,
    so they are not even counted as seen -- which is what distinguishes this
    from the closed-pull-request case below.
    """
    daemon = prepared(tmp_path, Recording())

    summary = await daemon.run_once()

    on_issues = [c for c in ISSUE_COMMENTS if "/pull/" not in c["html_url"]]
    assert len(on_issues) == 3
    assert summary.seen == len(OPEN_PULLS) + (len(ISSUE_COMMENTS) - len(on_issues))
    assert 87 not in [row["pr_number"] for row in rows(daemon)]


async def test_a_mention_on_a_closed_pull_request_is_dropped(tmp_path):
    """The recorded page holds a comment on pull request 60, long merged.

    It is not in the `/pulls?state=open` listing this very cycle produced, so
    the filter rejects it -- the real sequence, from a real payload, rather
    than a number the test invented.
    """
    daemon = prepared(tmp_path, Recording())

    await daemon.run_once()

    assert 60 not in [row["pr_number"] for row in rows(daemon)]
    assert daemon.open_pull_requests == frozenset({94, 95})


async def test_the_second_cycle_is_conditional_and_enqueues_nothing(tmp_path):
    handler = Recording()
    daemon = prepared(tmp_path, handler)

    first = await daemon.run_once()
    second = await daemon.run_once()

    assert first.enqueued == 3
    assert second.enqueued == 0
    assert len(rows(daemon)) == 3
    assert len(handler.calls) == 6


async def test_the_watermarks_advance_over_what_the_cycle_could_map(tmp_path):
    """Not the newest item on the page -- the newest one that *mapped*.

    The newest recorded comment is on issue 87, which is dropped in the
    payload mapping before the classifier or the watermark sees it, so the
    comments watermark stops at the newest comment on a *pull request*,
    an hour and twenty minutes earlier. That is the intended reading: a
    watermark is how far the classifier has got, and it never looked at the
    issue comment. It is also what makes it safe -- advancing past an item
    that was never classified is how a trigger goes missing.
    """
    daemon = prepared(tmp_path, Recording())

    await daemon.run_once()

    newest_pull = max(pull["created_at"] for pull in OPEN_PULLS)
    on_pulls = [c for c in ISSUE_COMMENTS if "/pull/" in c["html_url"]]
    newest_mapped = max(c["updated_at"] for c in on_pulls)
    newest_of_any = max(c["updated_at"] for c in ISSUE_COMMENTS)
    assert newest_mapped < newest_of_any

    assert daemon.store.watermark(PULLS_WM) == stamp(newest_pull)
    assert daemon.store.watermark(COMMENTS_WM) == stamp(newest_mapped)


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def test_a_watermark_at_the_newest_item_enqueues_it_again_next_cycle(tmp_path):
    """The same-second boundary, on recorded timestamps.

    Freshness is `created_at <= since`, so an item created in the same second
    the watermark was written is *not* fresh. Here the watermark is set to the
    newest recorded pull request, which must therefore be rejected while the
    older one stays rejected too.
    """
    daemon = make_daemon(tmp_path, Recording())
    newest = max(stamp(pull["created_at"]) for pull in OPEN_PULLS)
    daemon.store.advance_watermark(PULLS_WM, newest)
    daemon.store.advance_watermark(COMMENTS_WM, newest)

    summary = await daemon.run_once()

    assert summary.enqueued == 0
    assert rows(daemon) == []


async def test_one_second_earlier_lets_the_newest_pull_request_through(tmp_path):
    """The other side of the same boundary, so neither test passes alone."""
    daemon = make_daemon(tmp_path, Recording())
    newest = max(stamp(pull["created_at"]) for pull in OPEN_PULLS)
    daemon.store.advance_watermark(PULLS_WM, newest - timedelta(seconds=1))
    daemon.store.advance_watermark(COMMENTS_WM, newest)

    summary = await daemon.run_once()

    assert summary.enqueued == 1
    assert [row["pr_number"] for row in rows(daemon)] == [95]


# -- the single-pull-request read a claim makes --------------------------


async def test_the_recorded_single_pull_request_maps_to_checkout_facts(tmp_path):
    """`GET /pulls/{n}`, the one read a claimed trigger makes.

    It is not part of the poll cycle -- the worker makes it when it claims a
    row -- so it is driven through the same client here rather than through
    `run_once`. The counts matter as much as the sha: they are what the size
    gate is measured against, and they exist on no other payload.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pulls/93")
        return httpx.Response(200, json=PULL)

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    endpoints = RepoEndpoints(CONFIG.github.owner, CONFIG.github.name)

    facts = await fetch_pull_request_facts(client, endpoints, 93)

    assert facts.number == 93
    assert facts.head_sha == PULL["head"]["sha"]
    assert facts.base_ref == PULL["base"]["ref"]
    assert facts.changed_lines == PULL["additions"] + PULL["deletions"]
    assert facts.changed_files == PULL["changed_files"]
