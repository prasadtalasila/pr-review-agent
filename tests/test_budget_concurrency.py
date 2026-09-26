"""The reserve-then-settle guarantee, under real concurrent writers.

This is the test the whole storage choice exists for. Checking the remaining
allowance is not enough on its own: several workers can each observe
sufficient budget, each start a run, and collectively breach the cap while
every one of them checked correctly.

Threads rather than asyncio, because ``sqlite3`` calls block, and a separate
connection per thread, because a connection is not shareable across them.
Both are what make this a genuine test of SQLite's write lock rather than of
Python's.
"""

import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from pr_review_agent.budget import MENTION_ONLY_AT, Governor
from pr_review_agent.config import BudgetConfig
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import BudgetPolicy, SqliteStore
from pr_review_agent.triggers.models import Trigger, TriggerKind

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
REPO = "prasadtalasila/pr-review-agent"
OTHER_REPO = "prasadtalasila/other-repo"

WORKERS = 8
RUN_TOKENS = 1_000
# 40 % of 175 000 is 70 000; a seventh of that is the 10 000-token daily
# window, so ten runs would fill it exactly.
WEEKLY_TOKENS = 175_000

# But nine is the bound, not ten. Auto-review of fresh pull requests stops at
# the 85 % rung, which binds before the window itself does -- the remaining
# 15 % is deliberately held back for a maintainer's explicit @claude. Derived
# from the constant rather than written as 9, so a change to the rung fails
# here rather than silently widening what may be spent.
ADMISSIBLE = math.ceil(MENTION_ONLY_AT * (WEEKLY_TOKENS * 40 // 100 // 7) / RUN_TOKENS)


def config():
    return BudgetConfig(
        session_tokens=WEEKLY_TOKENS,
        weekly_tokens=WEEKLY_TOKENS,
        max_run_tokens=RUN_TOKENS,
        reviewer_share_pct=40,
    )


def trigger(pr, repo=REPO):
    """One trigger per pull request, so the per-PR lease never serialises us."""
    return Trigger(
        kind=TriggerKind.PR_OPENED,
        repo=repo,
        pr_number=pr,
        head_sha=f"sha{pr}",
        actor_id=114395272,
        dedupe_key=f"pr_opened:{repo}:{pr}:sha{pr}",
    )


def test_concurrent_workers_cannot_breach_a_window(tmp_path):
    path = tmp_path / "state.db"
    with SqliteStore(path) as seed:
        queue = ReviewQueue(seed, repo=REPO)
        for pr in range(WORKERS * 5):
            queue.enqueue(trigger(pr), now=NOON + timedelta(seconds=pr))

    def drain(name):
        """One worker: claim until the governor stops admitting."""
        claimed = 0
        with SqliteStore(path) as store:
            governor = Governor(store, config())
            queue = ReviewQueue(store, repo=REPO)
            while True:
                claim = queue.claim(now=NOON, owner=name, admit=governor.admit)
                if claim is None:
                    return claimed
                claimed += 1
                queue.complete(claim)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        admitted = sum(pool.map(drain, [f"w{n}" for n in range(WORKERS)]))

    assert admitted == ADMISSIBLE

    # And the ledger agrees: the reservations really were written, exactly
    # once each, inside the transactions that leased the rows.
    with sqlite3.connect(path) as conn:
        reserved = conn.execute(
            "SELECT COUNT(*), SUM(reserved_tokens) FROM ledger"
        ).fetchone()
    assert reserved == (ADMISSIBLE, ADMISSIBLE * RUN_TOKENS)
    assert reserved[1] <= config().daily_limit
    assert reserved[1] <= config().weekly_limit
    assert reserved[1] <= config().session_limit


def test_a_sequential_drain_admits_the_same_number(tmp_path):
    """The bound is the budget, not a race: one worker reaches it too.

    Without this, a concurrency test that accidentally serialised everything
    would still pass while proving nothing.
    """
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        queue = ReviewQueue(store, repo=REPO)
        for pr in range(WORKERS * 5):
            queue.enqueue(trigger(pr), now=NOON + timedelta(seconds=pr))
        governor = Governor(store, config())
        admitted = 0
        while (
            claim := queue.claim(now=NOON, owner="solo", admit=governor.admit)
        ) is not None:
            admitted += 1
            queue.complete(claim)

    assert admitted == ADMISSIBLE


def test_two_repos_over_one_store_share_one_pool(tmp_path):
    """The claim Phase 2 rests on: N daemons cannot collectively overspend.

    Each repository runs its own process with its own queue, and the pool is
    shared only because the ledger is. If either governed with its own copy
    of the numbers the bound would be ADMISSIBLE *per repo* -- twice what the
    plan actually funds -- so the assertion is that it stays ADMISSIBLE in
    total, exactly as for one repository draining alone.
    """
    path = tmp_path / "state.db"
    with SqliteStore(path) as seed:
        for repo in (REPO, OTHER_REPO):
            queue = ReviewQueue(seed, repo=repo)
            for pr in range(WORKERS * 5):
                queue.enqueue(trigger(pr, repo), now=NOON + timedelta(seconds=pr))
        # One authority publishes the pool arithmetic; the other complies.
        seed.publish_budget_policy(BudgetPolicy(REPO, config().shared()), now=NOON)

    def drain(worker):
        name, repo = worker
        claimed = 0
        with SqliteStore(path) as store:
            governor = Governor(store, config(), comply=repo != REPO)
            queue = ReviewQueue(store, repo=repo)
            while True:
                claim = queue.claim(now=NOON, owner=name, admit=governor.admit)
                if claim is None:
                    return claimed
                claimed += 1
                queue.complete(claim)

    workers = [(f"w{n}", REPO if n % 2 else OTHER_REPO) for n in range(WORKERS)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        admitted = sum(pool.map(drain, workers))

    assert admitted == ADMISSIBLE

    with sqlite3.connect(path) as conn:
        reserved = conn.execute(
            "SELECT COUNT(*), SUM(reserved_tokens) FROM ledger"
        ).fetchone()
    assert reserved == (ADMISSIBLE, ADMISSIBLE * RUN_TOKENS)
    assert reserved[1] <= config().weekly_limit
