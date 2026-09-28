"""The daemon suite's wiring: one config, one fake GitHub, one daemon.

Every `test_daemon_*.py` module builds its daemon here, against payload
helpers that carry the six fields the mapping reads. The recorded-payload
counterpart is `test_integration.py`, which replays pages GitHub actually
sent through the same `Daemon.run_once`.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx

from pr_review_agent.budget import Governor
from pr_review_agent.comments import AgentComments
from pr_review_agent.config import Config, GitHubConfig
from pr_review_agent.daemon import (
    COMMENTS,
    PULL_REQUESTS,
    Daemon,
)
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.poller.interval import AdaptiveInterval
from pr_review_agent.poller.poller import Poller
from pr_review_agent.publisher import Publisher
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.runs import RunStore
from pr_review_agent.store import SqliteStore

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
RECENT = NOW - timedelta(minutes=5)
#: A pull request the watermark has already moved past, for the tests that
#: need one open but not classified. Strictly older than the watermark
#: rather than equal to it: the classifier compares strictly, so an item
#: stamped the watermark second is deliberately re-offered (issue #73).
SEEN = OLD - timedelta(days=1)

ALICE_ID = 7
ALICE = {"id": ALICE_ID, "login": "alice", "type": "User"}

BUDGET = {
    "session_tokens": 88_000,
    "weekly_tokens": 1_500_000,
    "max_run_tokens": 60_000,
}

CONFIG = Config.from_mapping(
    {
        "github": {"repo": "o/r"},
        "triggers": {"allowlist": [ALICE_ID], "handle": "claude"},
        "budget": BUDGET,
        "engine": {
            "model": "claude-sonnet-5",
            "expected_version": "2.1.274",
            "timeout_seconds": 900,
        },
    }
)


#: The two watermark keys ``CONFIG``'s repository writes. Qualified by repo,
#: so that several daemons sharing one store for one budget do not overwrite
#: each other's high-water marks.
PULLS_WM = f"{PULL_REQUESTS}:o/r"
COMMENTS_WM = f"{COMMENTS}:o/r"

#: A second repository, for the case one store serves several daemons.
OTHER = replace(CONFIG, github=GitHubConfig(repo="other/repo"))
OTHER_PULLS_WM = f"{PULL_REQUESTS}:other/repo"


def stamp(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def pr_item(number: int, created_at: datetime) -> dict:
    return {
        "number": number,
        "head": {"sha": f"sha{number}"},
        "user": ALICE,
        "created_at": stamp(created_at),
        "draft": False,
    }


def issue_comment(comment_id: int, updated_at: datetime) -> dict:
    return {
        "id": comment_id,
        "user": ALICE,
        "body": "@claude review this",
        "updated_at": stamp(updated_at),
        "issue_url": "https://api.github.com/repos/o/r/issues/12",
        "html_url": "https://github.com/o/r/pull/12#issuecomment-1",
    }


def review_comment(comment_id: int, updated_at: datetime) -> dict:
    return {
        "id": comment_id,
        "user": ALICE,
        "body": "@claude here too",
        "updated_at": stamp(updated_at),
        "pull_request_url": "https://api.github.com/repos/o/r/pulls/12",
    }


def responder(pulls=None, issue_comments=None, review_comments=None):
    """A MockTransport handler: a body means 200, ``None`` means 304."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/pulls/comments" in url:
            body = review_comments
        elif "/issues/comments" in url:
            body = issue_comments
        else:
            body = pulls
        if body is None:
            return httpx.Response(304)
        return httpx.Response(200, json=body, headers={"etag": '"e"'})

    return handler


def make_daemon(tmp_path, handler, config=CONFIG, store=None) -> Daemon:
    # ``store`` is passed in only to put two repositories on one file, which
    # is how a shared budget is deployed.
    store = SqliteStore(tmp_path / "state.db") if store is None else store
    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    endpoints = RepoEndpoints(config.github.owner, config.github.name)
    poller = Poller(
        client=client,
        endpoints=endpoints,
        etags=store,
        # Zero keeps run_forever's wait instant; run_once ignores it.
        interval=AdaptiveInterval(min_seconds=0, max_seconds=0),
    )
    return Daemon(
        config=config,
        poller=poller,
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


def queued(daemon: Daemon) -> int:
    with daemon.store.transaction() as conn:
        return conn.execute("SELECT count(*) FROM queue").fetchone()[0]


# The command-line entry points are exercised in tests/test_cli.py, which
# owns the whole `pr-review-agent <noun> <verb>` tree.


# -- SIGHUP: the kill switch must not need a restart ---------------------


def config_yaml(
    enabled="true", repo="o/r", dry_run="false", authority="true", weekly="1500000"
):
    return (
        f"github:\n  repo: {repo}\n"
        f"triggers:\n  handle: claude\n  allowlist:\n    - {ALICE_ID}\n"
        f"publish:\n  dry_run: {dry_run}\n"
        f"budget:\n  enabled: {enabled}\n"
        f"  authority: {authority}\n"
        "  session_tokens: 88000\n"
        f"  weekly_tokens: {weekly}\n"
        "  max_run_tokens: 60000\n"
        "engine:\n"
        "  model: claude-sonnet-5\n"
        "  expected_version: '2.1.274'\n"
        "  timeout_seconds: 900\n"
    )


def daemon_with_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    daemon = make_daemon(tmp_path, {})
    daemon.config_path = path
    return daemon, path


# -- the shared budget policy: whose numbers govern one store ------------


def with_budget(config=CONFIG, **overrides):
    return replace(config, budget=replace(config.budget, **overrides))
