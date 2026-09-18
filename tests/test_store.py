"""SqliteStore: what has to survive a restart, and what must never go back."""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from pr_review_agent import store as store_module
from pr_review_agent.store import SCHEMA_VERSION, SqliteStore

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def test_etag_survives_a_reopen(tmp_path):
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        store.set("/repos/o/r/pulls", '"v1"')
    with SqliteStore(path) as store:
        assert store.get("/repos/o/r/pulls") == '"v1"'


def test_unknown_path_is_a_cold_start(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        assert store.get("/repos/o/r/pulls") is None


def test_setting_none_forgets_the_etag(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        store.set("/p", '"v1"')
        store.set("/p", None)
        assert store.get("/p") is None


def test_etag_is_overwritten_not_duplicated(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        store.set("/p", '"v1"')
        store.set("/p", '"v2"')
        assert store.get("/p") == '"v2"'


def test_watermark_survives_a_reopen(tmp_path):
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        store.advance_watermark("comments", NOON)
    with SqliteStore(path) as store:
        assert store.watermark("comments") == NOON


def test_watermark_never_moves_backwards(tmp_path):
    # An out-of-order settle must not re-admit events already decided --
    # for comments that would replay an old @claude and spend the allowance.
    with SqliteStore(tmp_path / "state.db") as store:
        store.advance_watermark("comments", NOON)
        in_force = store.advance_watermark("comments", NOON - timedelta(hours=1))
        assert in_force == NOON
        assert store.watermark("comments") == NOON


def test_watermark_moves_forward(tmp_path):
    later = NOON + timedelta(minutes=5)
    with SqliteStore(tmp_path / "state.db") as store:
        store.advance_watermark("comments", NOON)
        assert store.advance_watermark("comments", later) == later


def test_watermarks_are_independent_per_name(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        store.advance_watermark("comments", NOON)
        assert store.watermark("pull_requests") is None


def test_naive_watermark_is_rejected(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store, pytest.raises(ValueError):
        store.advance_watermark("comments", datetime(2026, 9, 17, 12, 0))


def test_non_utc_watermark_is_normalised(tmp_path):
    berlin = timezone(timedelta(hours=2))
    with SqliteStore(tmp_path / "state.db") as store:
        store.advance_watermark("comments", NOON.astimezone(berlin))
        assert store.watermark("comments") == NOON


def test_a_fresh_database_is_at_the_current_schema_version(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        assert store.schema_version == SCHEMA_VERSION


def test_a_pre_versioning_database_adopts_the_migrations(tmp_path):
    # The store shipped before the migration list existed, so a database in
    # the wild already has the first migration's tables at user_version 0.
    path = tmp_path / "state.db"
    legacy = sqlite3.connect(path, isolation_level=None)
    legacy.executescript(
        "CREATE TABLE etags (path TEXT PRIMARY KEY, etag TEXT NOT NULL);"
        "CREATE TABLE watermarks (name TEXT PRIMARY KEY, at TEXT NOT NULL);"
        "INSERT INTO etags VALUES ('/p', '\"v1\"');"
    )
    legacy.close()

    with SqliteStore(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.get("/p") == '"v1"'


def test_reopening_does_not_lose_data_to_a_re_migration(tmp_path):
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        store.set("/p", '"v1"')
    with SqliteStore(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.get("/p") == '"v1"'


def test_a_transaction_commits(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        with store.transaction() as conn:
            conn.execute("INSERT INTO etags VALUES ('/p', '\"v1\"')")
        assert store.get("/p") == '"v1"'


def test_a_failed_transaction_rolls_back(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        store.set("/p", '"v1"')
        with pytest.raises(RuntimeError), store.transaction() as conn:
            conn.execute("DELETE FROM etags")
            raise RuntimeError("the worker died mid-claim")
        assert store.get("/p") == '"v1"'


def test_the_ledger_arrives_with_the_schema(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        assert store.schema_version == SCHEMA_VERSION == 8
        with store.transaction() as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(ledger)").fetchall()
            }
    # usage_confidence exists because some engines report no tokens at all,
    # and an operator has to be able to see when that was the case.
    assert {"reserved_tokens", "used_tokens", "usage_confidence", "mode"} <= columns


def test_an_existing_database_adopts_the_ledger(tmp_path):
    """A store written before this migration gains the table, not an error."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        store.advance_watermark("comments", datetime(2026, 1, 1, tzinfo=timezone.utc))
        with store.transaction() as conn:
            conn.execute("DROP TABLE ledger")
            # Migration 6's columns go too, for the reason spelled out in
            # the contributor-index test below: a rewound version replays
            # every later migration, and an ALTER cannot run twice.
            conn.execute("ALTER TABLE queue DROP COLUMN comment_id")
            conn.execute("ALTER TABLE queue DROP COLUMN comment_source")
            conn.execute("PRAGMA user_version = 2")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.watermark("comments") is not None


def test_the_ledger_is_indexed_by_contributor(tmp_path):
    """The per-contributor window queries by actor over a trailing window."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list(ledger)")}
    assert "ledger_by_actor" in names


def test_an_existing_database_adopts_the_contributor_index(tmp_path):
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("DROP INDEX ledger_by_actor")
        # Migrations 5, 6 and 7 go too: rewinding the version without undoing
        # what came after it would replay an ALTER against a table that
        # already has the column, which is a state no real database reaches.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_lines")
        conn.execute("ALTER TABLE ledger DROP COLUMN stop_reason")
        conn.execute("ALTER TABLE queue DROP COLUMN comment_id")
        conn.execute("ALTER TABLE queue DROP COLUMN comment_source")
        conn.execute("PRAGMA user_version = 3")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        with reopened.transaction() as conn:
            names = {row[1] for row in conn.execute("PRAGMA index_list(ledger)")}
    assert "ledger_by_actor" in names


def test_an_existing_database_adopts_the_reviewed_lines_column(tmp_path):
    """A v4 store gains the estimator's predictor column, not an error.

    Existing rows keep a NULL, which is what stops a run recorded before the
    column existed from being fitted as "spent N tokens on zero lines".
    """
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_lines")
        # Migrations 6 and 7 go too, for the reason above.
        conn.execute("ALTER TABLE ledger DROP COLUMN stop_reason")
        conn.execute("ALTER TABLE queue DROP COLUMN comment_id")
        conn.execute("ALTER TABLE queue DROP COLUMN comment_source")
        conn.execute(
            "INSERT INTO ledger (dedupe_key, owner, actor_id, mode, "
            "reserved_tokens, reserved_at) VALUES ('k', 'w', 1, 'full', 10, 'x')"
        )
        conn.execute("PRAGMA user_version = 4")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        with reopened.transaction() as conn:
            assert conn.execute("SELECT reviewed_lines FROM ledger").fetchone() == (
                None,
            )


def test_an_existing_database_adopts_the_stop_reason_column(tmp_path):
    """A v5 store gains the column that says why a run ended.

    Rows written before it existed keep a NULL, which is the honest answer:
    nothing recorded why they stopped, and inventing a reason for them would
    put fiction in the one table that is never pruned.
    """
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("ALTER TABLE ledger DROP COLUMN stop_reason")
        # Migration 7's columns go with it, for the reason above.
        conn.execute("ALTER TABLE queue DROP COLUMN comment_id")
        conn.execute("ALTER TABLE queue DROP COLUMN comment_source")
        conn.execute("PRAGMA user_version = 5")
        conn.execute(
            "INSERT INTO ledger (dedupe_key, owner, actor_id, mode, "
            "reserved_tokens, reserved_at) VALUES ('k', 'w', 1, 'full', 10, 'x')"
        )

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        with reopened.transaction() as conn:
            assert conn.execute("SELECT stop_reason FROM ledger").fetchone() == (None,)


def test_a_failed_migration_leaves_the_version_behind(tmp_path):
    """The script and its version bump commit together, or neither does.

    Without that, a crash between them would leave a half-applied schema at
    a version claiming it was finished -- and ``ALTER TABLE ADD COLUMN``,
    unlike every ``CREATE`` above it, cannot be written to tolerate a replay.
    """
    path = tmp_path / "state.db"
    SqliteStore(path).close()

    broken = (*store_module._MIGRATIONS, "CREATE TABLE ok (a INTEGER); NOT SQL;")
    with (
        mock.patch.object(store_module, "_MIGRATIONS", broken),
        pytest.raises(sqlite3.OperationalError),
    ):
        SqliteStore(path)

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        with reopened.transaction() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
    assert "ok" not in tables


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


def test_an_existing_database_adopts_the_runs_table(tmp_path):
    """A v7 store gains it, and its queued rows survive."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        store.advance_watermark("comments", datetime(2026, 1, 1, tzinfo=timezone.utc))
        with store.transaction() as conn:
            conn.execute("DROP TABLE runs")
            conn.execute("PRAGMA user_version = 7")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.watermark("comments") is not None
