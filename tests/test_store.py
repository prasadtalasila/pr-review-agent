"""SqliteStore: what has to survive a restart, and what must never go back."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

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


def test_the_ledger_arrives_at_schema_version_three(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        assert store.schema_version == SCHEMA_VERSION == 3
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
            conn.execute("PRAGMA user_version = 2")

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == 3
        assert reopened.watermark("comments") is not None
