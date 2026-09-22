# Daemon Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run `poll_once()` on the adaptive interval, feed changed payloads through the classifier, and enqueue accepted triggers — so an accepted trigger becomes a durable queue row with no human in the loop.

**Architecture:** One `Daemon` dataclass over the components that already exist. `run_once()` performs one sequential cycle (poll → classify → enqueue → advance watermarks) and is where every interesting assertion lives; `run_forever()` is a thin wrapper that waits on a stop `Event` with the adaptive interval as its timeout. The loop stops at `enqueue`: it claims nothing, calls no review engine and spends no tokens.

**Tech Stack:** Python 3.10–3.14, asyncio, httpx (`MockTransport` in tests), SQLite via the existing `SqliteStore`, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-09-17-daemon-loop-design.md`

## Global Constraints

- **Nothing may call a review engine outside the budget governor.** This branch must not call `ReviewQueue.claim()` at all. (`CLAUDE.md` §5, `DESIGN.md` "The one rule")
- **Allowlisting is on the numeric GitHub user id, never the login.** No new trust check is introduced here; do not add one.
- **PR bodies, comment bodies and diffs are untrusted input.** Nothing in this branch may widen what the agent is allowed to do.
- Supported Python range is `>=3.10,<3.15`; `target-version = "py310"`. No 3.11+ syntax. `enum.StrEnum` comes from `._compat`, never from `enum`.
- Line length 88 (ruff). Ruff lint rules: `E`, `F`, `I`, `UP`, `B`, `SIM`.
- Pyright runs over **`src` and `tests`** in `basic` mode — test helpers must type-check too. Do not pass duck-typed fakes where a concrete type is annotated.
- Pylint must score ≥ 9.0 on `src` and on `tests`.
- The full local gate before claiming done: `poetry run pytest`, `poetry run ruff check .`, `poetry run ruff format --check .`, `poetry run pylint src --rcfile=.pylintrc --fail-under=9.0`, `poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 --disable=missing-function-docstring,missing-module-docstring`, `poetry run pyright src tests`. Quote the result; never predict it.
- The suite needs no network and spends no tokens. Keep it that way.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: Comment freshness — close the comments-watermark gap

`Comment` carries no timestamp and `_decide_comment` has no freshness check, so the `comments` watermark that `STORAGE.md` specifies cannot be applied. Until this lands, a fresh database replays every historical `@claude` as a new request. This change only ever **narrows** what triggers a review.

**Files:**
- Modify: `src/pr_review_agent/triggers/models.py` (the `Comment` dataclass)
- Modify: `src/pr_review_agent/poller/payloads.py` (the `comments` generator)
- Modify: `src/pr_review_agent/triggers/classifier.py` (`_decide_comment`, `classify_comment` docstring)
- Modify: `docs/TRIGGERS.md`
- Test: `tests/test_classifier.py`, `tests/test_payloads.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Comment.updated_at: datetime` (required, positioned before the defaulted `head_sha`). Task 3 reads it as the value the `comments` watermark advances on. `Classifier._decide_comment` gains the reason string `"not_fresh"`, which is already a member of `NOISY_REASONS`.

- [ ] **Step 1: Write the failing classifier test**

In `tests/test_classifier.py`, add `updated_at` to the comment factory and add the new test. The factory default must be `LATER` so every existing comment test keeps passing:

```python
def make_comment(author=ALICE, body="@claude review", comment_id=99, updated_at=LATER):
    return Comment(
        repo="prasadtalasila/pr-review-agent",
        pr_number=7,
        comment_id=comment_id,
        author=author,
        body=body,
        updated_at=updated_at,
    )


def test_comment_older_than_watermark_is_not_fresh(classifier):
    decision = classifier.classify_comment(make_comment(updated_at=EARLIER))
    assert not decision.accepted
    assert decision.reason == "not_fresh"


def test_comment_edited_after_the_watermark_is_accepted(classifier):
    decision = classifier.classify_comment(make_comment(updated_at=LATER))
    assert decision.accepted
    assert decision.trigger.kind is TriggerKind.MENTION
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_classifier.py -q`
Expected: FAIL — `TypeError: Comment.__init__() got an unexpected keyword argument 'updated_at'`.

- [ ] **Step 3: Add `updated_at` to the model**

In `src/pr_review_agent/triggers/models.py`, extend the `Comment` docstring and add the field. It is required, so it must sit **before** the defaulted `head_sha`:

```python
@dataclass(frozen=True)
class Comment:
    """A PR conversation comment or an inline diff comment.

    ``head_sha`` is optional because the issue-comments payload does not
    carry one: a conversation comment is attached to the pull request, not
    to a commit. Resolving it here would cost one extra API call per comment
    on every poll, so it is left unresolved and read at claim time instead --
    which is also the only moment at which it is still correct.

    ``updated_at`` is what the ``comments`` watermark advances on. Both
    comment endpoints are sorted by it and it only ever moves forward, so one
    high-water mark cannot hide a comment that surfaces later. An edit bumps
    it, which is deliberate: editing ``@claude`` into an existing comment is
    a request. A comment that was already a mention is stopped from being
    reviewed twice by its dedupe key, not by the watermark.
    """

    repo: str
    pr_number: int
    comment_id: int
    author: Actor
    body: str
    updated_at: datetime
    head_sha: str | None = None
```

- [ ] **Step 4: Add the freshness check to the classifier**

In `src/pr_review_agent/triggers/classifier.py`, insert the check into `_decide_comment` after the bot check and before the mention scan — a timestamp comparison is cheaper than a regex, and `not_fresh` is already in `NOISY_REASONS` so it stays at `DEBUG`:

```python
    def _decide_comment(self, comment: Comment) -> Decision:
        if self._is_self(comment.author):
            return Decision(None, "self_commenter")
        if comment.author.is_bot:
            return Decision(None, "bot_commenter")
        if comment.updated_at <= self.since:
            return Decision(None, "not_fresh")
        if not has_mention(comment.body, self.handle):
            return Decision(None, "no_mention")
        if not self.allowlist.allows(comment.author):
            return Decision(None, "commenter_not_allowlisted")
```

Leave the rest of the method unchanged. Then extend the `classify_comment` docstring with a second paragraph:

```python
        Freshness *is* checked, against the same ``since`` watermark the
        pull-request path uses. Without it a fresh database replays every
        historical mention as a new request. An edit bumps ``updated_at``, so
        editing ``@claude`` into an old comment does summon a review -- which
        is the correct reading of an allowlisted maintainer's intent.
```

- [ ] **Step 5: Run the classifier tests**

Run: `poetry run pytest tests/test_classifier.py -q`
Expected: PASS.

- [ ] **Step 6: Write the failing payload test**

In `tests/test_payloads.py`, add `updated_at` to both comment factories and assert it is mapped:

```python
def issue_comment(**overrides) -> dict:
    item = {
        "id": 555,
        "user": ALICE,
        "body": "@claude please look",
        "updated_at": "2026-09-17T08:00:00Z",
        "issue_url": "https://api.github.com/repos/o/r/issues/12",
        "html_url": "https://github.com/o/r/pull/12#issuecomment-555",
    }
    return {**item, **overrides}


def review_comment(**overrides) -> dict:
    item = {
        "id": 777,
        "user": ALICE,
        "body": "@claude here too",
        "updated_at": "2026-09-17T09:00:00Z",
        "pull_request_url": "https://api.github.com/repos/o/r/pulls/12",
        "commit_id": "cafebabe",
    }
    return {**item, **overrides}


def test_comment_updated_at_is_mapped():
    (comment,) = comments(REPO, [issue_comment()])
    assert comment.updated_at == datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)


def test_comment_without_updated_at_is_skipped():
    assert list(comments(REPO, [issue_comment(updated_at=None)])) == []
```

- [ ] **Step 7: Run the payload tests to verify they fail**

Run: `poetry run pytest tests/test_payloads.py -q`
Expected: FAIL — `Comment.__init__()` is missing the required `updated_at`.

- [ ] **Step 8: Map `updated_at` in the payload seam**

In `src/pr_review_agent/poller/payloads.py`, add one line to the `Comment(...)` construction inside `comments`. A missing or unparseable value raises inside the existing `try`, so the item is skipped with a warning like any other unmappable one — no new error handling:

```python
            yield Comment(
                repo=repo,
                pr_number=number,
                comment_id=int(item["id"]),
                author=Actor.from_api(item.get("user")),
                body=item.get("body") or "",
                updated_at=parse_timestamp(item["updated_at"]),
            )
```

Note `parse_timestamp(None)` raises `AttributeError`, which is **not** in the existing `except` tuple. Add `AttributeError` to it:

```python
        except (PayloadError, KeyError, TypeError, ValueError, AttributeError) as exc:
            _skip("comment", item, exc)
```

- [ ] **Step 9: Run the payload tests**

Run: `poetry run pytest tests/test_payloads.py -q`
Expected: PASS.

- [ ] **Step 10: Document the rule in TRIGGERS.md**

Add a subsection to `docs/TRIGGERS.md` explaining that a comment is only considered when `updated_at` is after the `comments` watermark, that an edit bumps `updated_at` so editing `@claude` in does summon a review, and that the dedupe key rather than the watermark is what stops a re-review. Match the surrounding heading style (an emoji + title).

- [ ] **Step 11: Run the whole suite and commit**

Run: `poetry run pytest -q`
Expected: PASS, no failures.

```bash
git add src/pr_review_agent/triggers/models.py \
        src/pr_review_agent/triggers/classifier.py \
        src/pr_review_agent/poller/payloads.py \
        tests/test_classifier.py tests/test_payloads.py docs/TRIGGERS.md
git commit -m "Apply the comments watermark to comment classification"
```

---

### Task 2: A `store:` section in config.yaml

The daemon needs to know where the SQLite file lives. The section is **optional** with a default of `state.db`: a missing path provably cannot spend tokens once cold-start seeding bounds a fresh database, and keeping it optional avoids editing the eight shared fixtures in `tests/test_config.py` that the governor branch's `budget:` section will also touch.

**Files:**
- Modify: `src/pr_review_agent/config.py`
- Modify: `config.example.yaml`
- Modify: `docs/CONFIG.md`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `StoreConfig(path: str)` frozen dataclass, `DEFAULT_STORE_PATH = "state.db"`, and `Config.store: StoreConfig`. Task 4 reads `config.store.path`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_store_path_defaults_when_the_section_is_absent():
    assert Config.from_mapping(VALID).store.path == "state.db"


def test_store_path_is_read_from_the_section():
    data = {**VALID, "store": {"path": "/var/lib/agent/state.db"}}
    assert Config.from_mapping(data).store.path == "/var/lib/agent/state.db"


def test_blank_store_path_is_rejected():
    data = {**VALID, "store": {"path": "   "}}
    with pytest.raises(ConfigError, match="store.path"):
        Config.from_mapping(data)


def test_unknown_key_in_store_is_rejected():
    data = {**VALID, "store": {"paht": "state.db"}}
    with pytest.raises(ConfigError, match="unknown keys in 'store'"):
        Config.from_mapping(data)
```

Confirm `ConfigError` is already imported at the top of the file; add it to the existing import if it is not.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_config.py -q`
Expected: FAIL — `unknown top-level sections: ['store']`, and `AttributeError: 'Config' object has no attribute 'store'`.

- [ ] **Step 3: Add `StoreConfig`**

In `src/pr_review_agent/config.py`, add the constant next to the other module-level names and the dataclass after `TriggerConfig`:

```python
#: Relative to the working directory the daemon is started in, which is why
#: the daemon logs the resolved absolute path at startup.
DEFAULT_STORE_PATH = "state.db"


@dataclass(frozen=True)
class StoreConfig:
    """Where the SQLite state file lives."""

    path: str = DEFAULT_STORE_PATH

    @classmethod
    def parse(cls, data: dict) -> StoreConfig:
        """Validate the ``store`` section."""
        path = data.get("path", DEFAULT_STORE_PATH)
        if not isinstance(path, str) or not path.strip():
            raise ConfigError("store.path must be a non-empty path")
        return cls(path=path)
```

- [ ] **Step 4: Wire it into `Config`**

Add the field to the `Config` dataclass, after `triggers`:

```python
    github: GitHubConfig
    triggers: TriggerConfig
    store: StoreConfig
```

And in `from_mapping`, widen the accepted top-level set and parse the section. The conditional expression keeps `_section` unchanged — that function is shared with the governor branch, so leaving its signature alone avoids a merge conflict:

```python
        unknown = sorted(set(data) - {"github", "triggers", "store"})
        if unknown:
            raise ConfigError(f"unknown top-level sections: {unknown}")
        return cls(
            github=GitHubConfig.parse(
                _section(data, "github", {"repo", "agent_user_id"})
            ),
            triggers=TriggerConfig.parse(
                _section(data, "triggers", {"allowlist", "handle"})
            ),
            store=StoreConfig.parse(
                _section(data, "store", {"path"}) if "store" in data else {}
            ),
        )
```

- [ ] **Step 5: Run the config tests**

Run: `poetry run pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 6: Document the key**

Append to `config.example.yaml`:

```yaml
store:
  # Where the SQLite state file lives: watermarks, ETags and the review
  # queue. Optional; this is the default. A relative path is resolved
  # against the working directory the daemon starts in, so prefer an
  # absolute path under a service account's data directory in production.
  path: state.db
```

Add a matching entry to the key reference in `docs/CONFIG.md`, following the format already used there for `github` and `triggers`.

- [ ] **Step 7: Run the whole suite and commit**

Run: `poetry run pytest -q`
Expected: PASS.

```bash
git add src/pr_review_agent/config.py tests/test_config.py \
        config.example.yaml docs/CONFIG.md
git commit -m "Add the store section to config.yaml"
```

---

### Task 3: `Daemon.run_once()` — one poll-classify-enqueue cycle

The cycle itself, and every spend bound that matters. `run_forever` and the entry point come in Task 4.

**Files:**
- Create: `src/pr_review_agent/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `Comment.updated_at` (Task 1); `config.store.path` is *not* used here (Task 4 uses it).
- Produces:
  - `PULL_REQUESTS = "pull_requests"`, `COMMENTS = "comments"` — watermark names.
  - `CycleSummary(seen: int, enqueued: int)` frozen dataclass.
  - `Daemon(config: Config, poller: Poller, store: SqliteStore, queue: ReviewQueue)` dataclass.
  - `Daemon.seed_watermarks(*, now: datetime) -> None`
  - `Daemon.run_once() -> CycleSummary`
  - Task 4 adds `run_forever` and `main` to this same module.

- [ ] **Step 1: Write the test harness and the cold-start test**

Create `tests/test_daemon.py`:

```python
"""Daemon loop: what one cycle enqueues, and what it must never enqueue."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pr_review_agent.config import Config
from pr_review_agent.daemon import COMMENTS, PULL_REQUESTS, Daemon
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.poller.interval import AdaptiveInterval
from pr_review_agent.poller.poller import Poller
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import SqliteStore

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
RECENT = NOW - timedelta(minutes=5)
ALICE_ID = 7
ALICE = {"id": ALICE_ID, "login": "alice", "type": "User"}

CONFIG = Config.from_mapping(
    {
        "github": {"repo": "o/r", "agent_user_id": 42},
        "triggers": {"allowlist": [ALICE_ID], "handle": "claude"},
    }
)


def pr_item(number: int, created_at: datetime) -> dict:
    return {
        "number": number,
        "head": {"sha": f"sha{number}"},
        "user": ALICE,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "draft": False,
    }


def comment_item(comment_id: int, updated_at: datetime) -> dict:
    return {
        "id": comment_id,
        "user": ALICE,
        "body": "@claude review this",
        "updated_at": updated_at.isoformat().replace("+00:00", "Z"),
        "issue_url": "https://api.github.com/repos/o/r/issues/12",
        "html_url": "https://github.com/o/r/pull/12#issuecomment-1",
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


def make_daemon(tmp_path, handler, *, queue=None) -> Daemon:
    store = SqliteStore(tmp_path / "state.db")
    poller = Poller(
        client=GitHubClient(token="t", transport=httpx.MockTransport(handler)),
        endpoints=RepoEndpoints("o", "r"),
        etags=store,
        interval=AdaptiveInterval(min_seconds=0, max_seconds=0),
    )
    return Daemon(
        config=CONFIG,
        poller=poller,
        store=store,
        queue=queue if queue is not None else ReviewQueue(store),
    )


def queued(daemon: Daemon) -> int:
    with daemon.store.transaction() as conn:
        return conn.execute("SELECT count(*) FROM queue").fetchone()[0]


async def test_cold_start_enqueues_nothing_from_the_backlog(tmp_path):
    handler = responder(
        pulls=[pr_item(1, OLD), pr_item(2, OLD + timedelta(days=1))],
        issue_comments=[comment_item(11, OLD), comment_item(12, OLD)],
    )
    daemon = make_daemon(tmp_path, handler)
    daemon.seed_watermarks(now=NOW)

    summary = await daemon.run_once()

    assert summary.enqueued == 0
    assert queued(daemon) == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `poetry run pytest tests/test_daemon.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pr_review_agent.daemon'`.

- [ ] **Step 3: Write `daemon.py`**

Create `src/pr_review_agent/daemon.py`:

```python
"""Run the poller on a schedule and turn what it sees into queued work.

This is the wiring between two halves that already exist. The poller knows
what changed, the classifier knows what may be reviewed, and the queue knows
what has already been paid for. The loop stops at ``enqueue``: it claims
nothing and calls no review engine, so it spends no tokens. A claim is the
point at which work becomes expensive, and that is the budget governor's to
guard.

Three rules carry the correctness, and each one exists to stop allowance
being spent on work that was already decided.

**Cold start is the spend bound.** A watermark that has never been set is
seeded to the moment the daemon started, so nothing pre-dating the first
start is ever enqueued. Without it a fresh database re-offers the entire open
backlog as new pull requests and replays every historical ``@claude`` as a
new request.

**A watermark advances to the newest timestamp seen in the payload**, never
to wall-clock now. An item that exists but is not yet visible to the API
would otherwise fall into the gap between the two and be skipped forever.

**The watermark advances only after the enqueue.** A crash in between costs
one re-classification, which ``INSERT OR IGNORE`` makes free; the reverse
order loses the trigger permanently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .poller import payloads
from .poller.endpoints import Endpoint
from .poller.poller import PollCycle, Poller
from .queue import ReviewQueue
from .store import SqliteStore
from .triggers.models import Decision

logger = logging.getLogger(__name__)

#: The two watermark names declared in STORAGE.md. Both comment endpoints
#: feed ``COMMENTS``: GitHub's ``updated`` only moves forward, so one
#: high-water mark cannot hide a comment that surfaces later.
PULL_REQUESTS = "pull_requests"
COMMENTS = "comments"

COMMENT_ENDPOINTS = (Endpoint.ISSUE_COMMENTS, Endpoint.REVIEW_COMMENTS)


@dataclass(frozen=True)
class CycleSummary:
    """How much one cycle looked at, and how much of it was new work."""

    seen: int
    enqueued: int

    def __add__(self, other: CycleSummary) -> CycleSummary:
        return CycleSummary(
            seen=self.seen + other.seen,
            enqueued=self.enqueued + other.enqueued,
        )


EMPTY = CycleSummary(seen=0, enqueued=0)


@dataclass
class Daemon:
    """One repository's poll-classify-enqueue loop."""

    config: Config
    poller: Poller
    store: SqliteStore
    queue: ReviewQueue

    def seed_watermarks(self, *, now: datetime) -> None:
        """Bound a fresh database to ``now`` before the first poll.

        Called once at startup so the bound is process start rather than
        first-successful-poll, which would drift if the first polls fail.
        """
        for name in (PULL_REQUESTS, COMMENTS):
            self._since(name, now=now)

    async def run_once(self) -> CycleSummary:
        """Poll every endpoint once and enqueue whatever the classifier
        accepts."""
        cycle = await self.poller.poll_once()
        return self._process(cycle, now=datetime.now(timezone.utc))

    def _process(self, cycle: PollCycle, *, now: datetime) -> CycleSummary:
        changed = cycle.changed_items()
        summary = self._pull_requests(
            changed.get(Endpoint.OPEN_PULLS), now=now
        ) + self._comments(changed, now=now)
        logger.info(
            "cycle seen=%d enqueued=%d", summary.seen, summary.enqueued
        )
        return summary

    def _pull_requests(
        self, items: list[dict] | None, *, now: datetime
    ) -> CycleSummary:
        """Classify a changed ``/pulls`` payload and enqueue what it accepts."""
        if items is None:
            return EMPTY
        since = self._since(PULL_REQUESTS, now=now)
        classifier = self.config.classifier(since)
        newest, summary = since, EMPTY
        for pull in payloads.pull_requests(self.config.github.repo, items):
            newest = max(newest, pull.created_at)
            summary += self._enqueue(
                classifier.classify_pull_request(pull), now=now
            )
        self.store.advance_watermark(PULL_REQUESTS, newest)
        return summary

    def _comments(
        self, changed: dict[Endpoint, list[dict]], *, now: datetime
    ) -> CycleSummary:
        """Classify both changed comment payloads against one watermark."""
        batches = [changed[ep] for ep in COMMENT_ENDPOINTS if ep in changed]
        if not batches:
            return EMPTY
        since = self._since(COMMENTS, now=now)
        classifier = self.config.classifier(since)
        newest, summary = since, EMPTY
        for batch in batches:
            for comment in payloads.comments(self.config.github.repo, batch):
                newest = max(newest, comment.updated_at)
                summary += self._enqueue(
                    classifier.classify_comment(comment), now=now
                )
        self.store.advance_watermark(COMMENTS, newest)
        return summary

    def _enqueue(self, decision: Decision, *, now: datetime) -> CycleSummary:
        """One classified item: seen always, enqueued only when it is new."""
        if decision.trigger is None:
            return CycleSummary(seen=1, enqueued=0)
        added = self.queue.enqueue(decision.trigger, now=now)
        return CycleSummary(seen=1, enqueued=int(added))

    def _since(self, name: str, *, now: datetime) -> datetime:
        """The watermark in force for ``name``, seeding an unset one to
        ``now``."""
        stored = self.store.watermark(name)
        if stored is not None:
            return stored
        logger.info(
            "cold start: seeding the %s watermark to %s", name, now.isoformat()
        )
        return self.store.advance_watermark(name, now)
```

- [ ] **Step 4: Run the cold-start test**

Run: `poetry run pytest tests/test_daemon.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the cold-start bound**

```bash
git add src/pr_review_agent/daemon.py tests/test_daemon.py
git commit -m "Add the daemon cycle, bounded by cold-start watermark seeding"
```

- [ ] **Step 6: Write the remaining cycle tests**

Append to `tests/test_daemon.py`:

```python
async def test_seeding_sets_both_watermarks(tmp_path):
    daemon = make_daemon(tmp_path, responder())
    daemon.seed_watermarks(now=NOW)
    assert daemon.store.watermark(PULL_REQUESTS) == NOW
    assert daemon.store.watermark(COMMENTS) == NOW


async def test_seeding_does_not_rewind_an_existing_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULL_REQUESTS, NOW)
    daemon.seed_watermarks(now=OLD)
    assert daemon.store.watermark(PULL_REQUESTS) == NOW


async def test_a_pull_request_after_the_watermark_is_enqueued(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 1
    assert daemon.queue.status("pr_opened:o/r:3:sha3") is not None


async def test_repolling_the_same_payload_enqueues_once(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

    first = await daemon.run_once()
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)  # undo, force a re-look
    second = await daemon.run_once()

    assert (first.enqueued, second.enqueued) == (1, 0)
    assert queued(daemon) == 1


async def test_watermark_advances_to_the_newest_item_not_to_now(tmp_path):
    daemon = make_daemon(
        tmp_path,
        responder(pulls=[pr_item(3, RECENT), pr_item(4, RECENT - timedelta(hours=1))]),
    )
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)

    await daemon.run_once()

    assert daemon.store.watermark(PULL_REQUESTS) == RECENT


async def test_an_unchanged_endpoint_moves_no_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())  # every endpoint 304s
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)
    daemon.store.advance_watermark(COMMENTS, OLD)

    summary = await daemon.run_once()

    assert summary == EMPTY_SUMMARY
    assert daemon.store.watermark(PULL_REQUESTS) == OLD
    assert daemon.store.watermark(COMMENTS) == OLD


async def test_both_comment_endpoints_share_one_watermark(tmp_path):
    issue = comment_item(11, RECENT - timedelta(hours=2))
    review = dict(comment_item(12, RECENT), pull_request_url="https://api.github.com/repos/o/r/pulls/12")
    daemon = make_daemon(
        tmp_path, responder(issue_comments=[issue], review_comments=[review])
    )
    daemon.store.advance_watermark(COMMENTS, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 2
    assert daemon.store.watermark(COMMENTS) == RECENT


async def test_a_failing_enqueue_leaves_the_watermark_unmoved(tmp_path):
    class BrokenQueue(ReviewQueue):
        def enqueue(self, trigger, *, now):
            raise RuntimeError("disk full")

    store_path = tmp_path / "state.db"
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.queue = BrokenQueue(daemon.store)
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)
    assert store_path.exists()

    with pytest.raises(RuntimeError):
        await daemon.run_once()

    assert daemon.store.watermark(PULL_REQUESTS) == OLD
```

Add the module-level constant the 304 test uses, next to `NOW`:

```python
from pr_review_agent.daemon import EMPTY as EMPTY_SUMMARY
```

- [ ] **Step 7: Run them**

Run: `poetry run pytest tests/test_daemon.py -q`
Expected: PASS. If `test_both_comment_endpoints_share_one_watermark` fails on the review-comment fixture, check that `comment_item`'s `issue_url`/`html_url` keys are overridden by `pull_request_url` — `_pr_number` prefers `pull_request_url`, so both map to PR 12 and produce distinct dedupe keys from their distinct ids.

- [ ] **Step 8: Commit**

```bash
git add tests/test_daemon.py
git commit -m "Pin the daemon cycle's watermark and dedupe behaviour"
```

---

### Task 4: `run_forever()`, signal handling and the entry point

**Files:**
- Modify: `src/pr_review_agent/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `Daemon.run_once()` and `Daemon.seed_watermarks()` (Task 3); `config.store.path` and `StoreConfig` (Task 2).
- Produces: `Daemon.run_forever(stop: asyncio.Event) -> None`, `run(config: Config, token: str) -> None`, `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing loop tests**

Append to `tests/test_daemon.py`:

```python
async def test_the_loop_stops_without_waiting_out_the_interval(tmp_path):
    stop = asyncio.Event()
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        polls.append(str(request.url))
        stop.set()
        return httpx.Response(304)

    daemon = make_daemon(tmp_path, handler)
    await asyncio.wait_for(daemon.run_forever(stop), timeout=5)

    assert len(polls) == 3  # exactly one cycle, three endpoints


async def test_a_client_error_does_not_stop_the_loop(tmp_path):
    stop = asyncio.Event()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(500)
        stop.set()
        return httpx.Response(304)

    daemon = make_daemon(tmp_path, handler)
    await asyncio.wait_for(daemon.run_forever(stop), timeout=5)

    assert calls["n"] > 3  # it kept polling after the failed cycle


async def test_an_already_set_stop_runs_no_cycle(tmp_path):
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        polls.append(str(request.url))
        return httpx.Response(304)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(make_daemon(tmp_path, handler).run_forever(stop), timeout=5)

    assert polls == []
```

Add `import asyncio` to the test module's imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `poetry run pytest tests/test_daemon.py -q -k "loop or client_error or already_set"`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'run_forever'`.

- [ ] **Step 3: Add `run_forever` and `_wait`**

Add to the imports in `src/pr_review_agent/daemon.py`:

```python
import argparse
import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import Config, ConfigError
from .poller.client import GitHubClient, GitHubClientError
from .poller.endpoints import Endpoint, RepoEndpoints

TOKEN_ENV = "GITHUB_TOKEN"
```

Add the method to `Daemon`, directly after `run_once`:

```python
    async def run_forever(self, stop: asyncio.Event) -> None:
        """Cycle until ``stop`` is set.

        A ``GitHubClientError`` is logged and the cycle skipped: a transient
        network failure must not kill a daemon. Anything else propagates --
        an unexpected bug should crash loudly rather than spin silently.
        """
        while not stop.is_set():
            try:
                await self.run_once()
            except GitHubClientError as exc:
                logger.error("poll cycle failed, retrying after the interval: %s", exc)
            await _wait(stop, self.poller.interval.seconds)
```

And the module-level helper, after the `Daemon` class:

```python
async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or until ``stop`` is set -- whichever comes first.

    A plain ``sleep`` would make a ``SIGTERM`` arriving early in a 600 s idle
    interval hang a service restart for the remainder of it.
    """
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
```

- [ ] **Step 4: Run the loop tests**

Run: `poetry run pytest tests/test_daemon.py -q`
Expected: PASS.

- [ ] **Step 5: Add the entry point**

Append to `src/pr_review_agent/daemon.py`:

```python
def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Set ``stop`` on SIGINT or SIGTERM.

    ``add_signal_handler`` is the asyncio-aware route and wakes the loop
    immediately. It is unimplemented on Windows, which CI spot-checks, so the
    plain handler is the fallback there.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())


async def run(config: Config, token: str) -> None:
    """Build the daemon ``config`` describes and run it until stopped."""
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    path = Path(config.store.path).resolve()
    logger.info("state database: %s", path)
    client = GitHubClient(token)
    try:
        with SqliteStore(path) as store:
            daemon = Daemon(
                config=config,
                poller=Poller(
                    client=client,
                    endpoints=RepoEndpoints(config.github.owner, config.github.name),
                    etags=store,
                ),
                store=store,
                queue=ReviewQueue(store),
            )
            daemon.seed_watermarks(now=datetime.now(timezone.utc))
            await daemon.run_forever(stop)
    finally:
        await client.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the daemon from the command line; non-zero exit means unusable."""
    parser = argparse.ArgumentParser(description="pr-review-agent daemon")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"{TOKEN_ENV} is not set", file=sys.stderr)
        return 2
    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    asyncio.run(run(config, token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Test the entry point's failure modes**

Append to `tests/test_daemon.py`:

```python
def test_main_without_a_token_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert main(["--config", str(tmp_path / "config.yaml")]) == 2
    assert "GITHUB_TOKEN is not set" in capsys.readouterr().err


def test_main_with_an_unreadable_config_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert main(["--config", str(tmp_path / "missing.yaml")]) == 2
    assert "cannot read config" in capsys.readouterr().err
```

Add `main` to the `pr_review_agent.daemon` import at the top of the test module.

- [ ] **Step 7: Run the whole suite**

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 8: Run the full gate**

```bash
poetry run ruff format --check .
poetry run ruff check .
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 \
  --disable=missing-function-docstring,missing-module-docstring
poetry run pyright src tests
```

Fix anything reported. `ruff format .` reformats in place if the check fails.

- [ ] **Step 9: Commit**

```bash
git add src/pr_review_agent/daemon.py tests/test_daemon.py
git commit -m "Run the daemon loop until a signal stops it"
```

---

### Task 5: Documentation

**Files:**
- Create: `docs/DAEMON.md`
- Modify: `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/STORAGE.md`, `README.md`, `DEVELOPER.md`

**Interfaces:**
- Consumes: everything from Tasks 1–4. Produces no code.

- [ ] **Step 1: Write `docs/DAEMON.md`**

One component doc matching the house style of `QUEUE.md` and `POLLER.md` — emoji headings, prose that explains *why*, a table where a table helps. It must cover:

- what one cycle does, as the diagram from the spec;
- the three ordering rules: enqueue before advancing, advance to newest-seen not `now`, cold-start seeding as the spend bound;
- why both comment endpoints share one watermark, and that editing `@claude` into an old comment summons a review;
- the known consequence that a draft pull request is not auto-reviewed even after it is marked ready, and that the escape hatch is an allowlisted `@claude`;
- errors and shutdown: `GitHubClientError` is survived, bare `Exception` is not caught, `SIGTERM` does not wait out the interval;
- how to run it: `GITHUB_TOKEN=... poetry run python -m pr_review_agent.daemon`;
- what it deliberately does **not** do — claim, review, or publish — with a pointer to `BUDGET.md`.

- [ ] **Step 2: Update the status tables**

- `docs/ARCHITECTURE.md`: change the daemon-loop row to `implemented — [DAEMON.md](DAEMON.md)`; add it to the package-layout tree; replace the closing "⏱ The daemon loop" paragraph's last sentence, which says the wiring is "the remaining gap", with a pointer to `DAEMON.md`.
- `docs/ROADMAP.md`: move "Daemon loop calling `poll_once()` on a schedule" to `implemented, unit tested`; remove item 1 from "Next" and renumber; update the "Known gaps" entry if the comments-watermark gap is listed there.
- `docs/STORAGE.md`: note in the Watermarks section that `DAEMON.md` is what advances them, and that both comment endpoints share the `comments` mark.
- `README.md`: change the `Daemon loop` row to `implemented (`python -m pr_review_agent.daemon`)`.
- `DEVELOPER.md`: add a "Running the daemon" section after "Bootstrap checks", with the command, the `GITHUB_TOKEN` requirement, the `--config` flag and the exit codes.

- [ ] **Step 3: Verify the docs are accurate**

Re-read each changed claim against the code. Every command quoted must be one you have actually run.

- [ ] **Step 4: Run the full gate once more and commit**

```bash
poetry run pytest -q
git add docs/ README.md DEVELOPER.md
git commit -m "Document the daemon loop"
```

---

## Self-review

**Spec coverage.** Goal → Tasks 3–4. `store:` section → Task 2. Comments-watermark gap → Task 1. Cold-start bound → Task 3 Steps 1–5. Watermark ordering rules → Task 3 Step 3, pinned in Step 6. One watermark for two comment endpoints → Task 3 Step 6. Errors and shutdown → Task 4. Entry point → Task 4 Step 5. Every test named in the spec's "What the tests must pin" appears in Task 3 Step 6 or Task 4 Step 1, except `test_comment_older_than_watermark_is_not_fresh`, which is Task 1 Step 1. Docs → Task 5. The Celery rejection is already committed.

**Placeholders.** None: every code step carries the code, and the only prose-only steps are the documentation ones in Task 5, which enumerate required content rather than deferring it.

**Type consistency.** `CycleSummary(seen, enqueued)` is constructed identically in `_enqueue`, `_pull_requests`, `_comments` and `EMPTY`. `PULL_REQUESTS` / `COMMENTS` are the same string constants in `daemon.py` and `tests/test_daemon.py`. `Daemon(config=, poller=, store=, queue=)` matches between `make_daemon` in the tests and `run()` in Task 4. `StoreConfig.path` is written in Task 2 and read in Task 4. `Comment.updated_at` is written in Task 1 and read in `_comments` in Task 3.
