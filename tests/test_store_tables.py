"""Every table and column an existing database has had to grow into.

Each is asserted twice -- present in a fresh schema, and adopted by a
database created before it existed -- because an operator who deployed early
must not need a fresh database to get the next release.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from pr_review_agent.store import SCHEMA_VERSION, BudgetPolicy, SqliteStore


def test_the_queue_carries_the_comment_a_mention_came_from(tmp_path):
    """The publisher reacts on that comment, and the reaction URL depends on
    which endpoint it arrived on."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(queue)")}
    assert {"comment_id", "comment_source"} <= columns


def test_an_existing_database_adopts_the_comment_columns(tmp_path):
    """A v6 store gains them, and its queued rows survive with a NULL.

    A row enqueued before the columns existed names no comment, so the
    publisher falls back to reacting on the pull request rather than
    guessing an id.
    """
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("ALTER TABLE queue DROP COLUMN comment_id")
        conn.execute("ALTER TABLE queue DROP COLUMN comment_source")
        conn.execute(
            "INSERT INTO queue (dedupe_key, kind, repo, pr_number, actor_id, "
            "status, enqueued_at) VALUES ('k', 'mention', 'o/r', 1, 9, "
            "'pending', 'x')"
        )
        # Migrations 11 to 13 go too, for the reason above: a rewound
        # version replays every later migration, and an ALTER cannot
        # run twice.
        conn.execute("DROP INDEX ledger_by_pr")
        conn.execute("ALTER TABLE ledger DROP COLUMN repo")
        conn.execute("ALTER TABLE ledger DROP COLUMN pr_number")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_outcome")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
        # Migrations 16 to 18 go too: a rewound version replays them.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("PRAGMA user_version = 6")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        with reopened.transaction() as conn:
            row = conn.execute(
                "SELECT comment_id, comment_source FROM queue"
            ).fetchone()
    assert row == (None, None)


def test_runs_arrive_with_the_schema(tmp_path):
    """The only table holding review content, and so the only one purged."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert {"findings", "comment_id", "published_at", "content_purged_at"} <= columns
    # omitted is what the coverage footer names, kept for a retried post.
    assert "omitted" in columns


def test_an_existing_database_adopts_the_runs_table(tmp_path):
    """A v7 store gains it, and its queued rows survive."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        store.advance_watermark("comments", datetime(2026, 1, 1, tzinfo=timezone.utc))
        with store.transaction() as conn:
            conn.execute("DROP TABLE runs")
            # Migrations 11 and 12 go too, for the reason above: a rewound
            # version replays every later migration, and an ALTER cannot
            # run twice.
            conn.execute("DROP INDEX ledger_by_pr")
            conn.execute("ALTER TABLE ledger DROP COLUMN repo")
            conn.execute("ALTER TABLE ledger DROP COLUMN pr_number")
            # Migrations 16 to 18 go too: a rewound version replays them.
            conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
            conn.execute("PRAGMA user_version = 7")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.watermark("comments") is not None


def test_the_breaker_state_arrives_with_the_schema(tmp_path):
    """Where the circuit breaker persists what it has learned."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(budget_state)")}
    assert columns == {"key", "value"}


def test_an_existing_database_adopts_the_breaker_state(tmp_path):
    """A v8 store gains it, empty -- which is what "never tripped" reads as."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("DROP TABLE budget_state")
        # Migrations 11 to 13 go too, for the reason above: a rewound
        # version replays every later migration, and an ALTER cannot
        # run twice.
        conn.execute("DROP INDEX ledger_by_pr")
        conn.execute("ALTER TABLE ledger DROP COLUMN repo")
        conn.execute("ALTER TABLE ledger DROP COLUMN pr_number")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_outcome")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
        # Migrations 16 to 18 go too: a rewound version replays them.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("PRAGMA user_version = 8")

    with SqliteStore(path) as reopened, reopened.transaction() as conn:
        assert reopened.schema_version == SCHEMA_VERSION
        assert conn.execute("SELECT * FROM budget_state").fetchall() == []


def test_the_budget_policy_arrives_with_the_schema(tmp_path):
    """Where an authority publishes the pool arithmetic others adopt."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(budget_policy)")}
    assert columns == {"id", "authority_repo", "policy", "written_at"}


def test_an_existing_database_adopts_the_budget_policy(tmp_path):
    """A v9 store gains it, empty -- which is what "no authority yet" reads as."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("DROP TABLE budget_policy")
        # Migrations 11 to 13 go too, for the reason above: a rewound
        # version replays every later migration, and an ALTER cannot
        # run twice.
        conn.execute("DROP INDEX ledger_by_pr")
        conn.execute("ALTER TABLE ledger DROP COLUMN repo")
        conn.execute("ALTER TABLE ledger DROP COLUMN pr_number")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_outcome")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
        # Migrations 16 to 18 go too: a rewound version replays them.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("PRAGMA user_version = 9")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.budget_policy() is None


def test_the_budget_policy_holds_one_row(tmp_path):
    """A second policy would be a second opinion about one shared pool."""
    with (
        SqliteStore(tmp_path / "state.db") as store,
        store.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError),
    ):
        conn.execute(
            "INSERT INTO budget_policy (id, authority_repo, policy, written_at) "
            "VALUES (2, 'o/r', '{}', '2026-01-01T00:00:00+00:00')"
        )


def test_publishing_a_policy_replaces_the_previous_one(tmp_path):
    """An authority republishes on every start and every reload."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SqliteStore(tmp_path / "state.db") as store:
        store.publish_budget_policy(
            BudgetPolicy("o/r", {"weekly_tokens": 1_000}), now=now
        )
        store.publish_budget_policy(
            BudgetPolicy("o/r", {"weekly_tokens": 2_000}), now=now
        )
        published = store.budget_policy()

    assert published == BudgetPolicy("o/r", {"weekly_tokens": 2_000})


def test_the_publish_attempt_columns_arrive_with_the_schema(tmp_path):
    """What bounds a post that GitHub will never accept."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert {"publish_attempts", "publish_failed_at"} <= columns


def test_an_existing_database_adopts_the_publish_attempt_columns(tmp_path):
    """A v12 store gains them, at zero -- nothing counted the posts before."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute(
            "INSERT INTO runs (dedupe_key, repo, pr_number, head_sha, outcome, "
            "findings, recorded_at) VALUES ('k', 'o/r', 7, 'abc', 'completed', "
            "'[]', '2026-09-27T12:00:00+00:00')"
        )
        conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
        # Migrations 16 to 18 go too: a rewound version replays them.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("PRAGMA user_version = 12")

    with SqliteStore(path) as reopened, reopened.transaction() as conn:
        assert reopened.schema_version == SCHEMA_VERSION
        assert conn.execute(
            "SELECT publish_attempts, publish_failed_at FROM runs"
        ).fetchall() == [(0, None)]


def test_the_agent_comment_table_arrives_with_the_schema(tmp_path):
    """What stops the agent answering its own review comment."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_comments)")}
    assert columns == {"repo", "comment_id", "posted_at"}


def test_an_existing_database_adopts_the_agent_comment_table(tmp_path):
    """A v13 store gains it, empty -- which is what a first run reads as."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("DROP TABLE agent_comments")
        # Migrations 16 to 18 go too: a rewound version replays them.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("PRAGMA user_version = 13")

    with SqliteStore(path) as reopened, reopened.transaction() as conn:
        assert reopened.schema_version == SCHEMA_VERSION
        assert conn.execute("SELECT * FROM agent_comments").fetchall() == []
