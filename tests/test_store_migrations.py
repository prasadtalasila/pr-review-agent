"""The migration machinery: versioned, idempotent, and all-or-nothing.

A database that predates versioning adopts the migrations; a failed one
leaves the version behind so the next open retries it rather than skipping
it.
"""

import sqlite3
from datetime import datetime, timezone
from unittest import mock

import pytest

from pr_review_agent import store as store_module
from pr_review_agent.store import SCHEMA_VERSION, SqliteStore


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


def test_the_ledger_arrives_with_the_schema(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        assert store.schema_version == SCHEMA_VERSION == 19
        with store.transaction() as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(ledger)").fetchall()
            }
    # usage_confidence exists because some engines report no tokens at all,
    # and an operator has to be able to see when that was the case.
    assert {"reserved_tokens", "used_tokens", "usage_confidence", "mode"} <= columns
    # reviewed_since keeps incremental rounds out of the pre-flight fit.
    assert "reviewed_since" in columns


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
            # Migration 12's and 13's columns go too, for the same reason.
            # Migration 11's went with the table above.
            conn.execute("ALTER TABLE runs DROP COLUMN publish_outcome")
            conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
            conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
            # Migrations 17 to 19 go too, for the same reason.
            conn.execute("ALTER TABLE runs DROP COLUMN omitted")
            conn.execute("ALTER TABLE runs DROP COLUMN assessment")
            conn.execute("ALTER TABLE queue DROP COLUMN command")
            conn.execute("ALTER TABLE runs DROP COLUMN description")
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
        # Migrations 11 to 13 go too, for the reason above: a rewound
        # version replays every later migration, and an ALTER cannot
        # run twice.
        conn.execute("DROP INDEX ledger_by_pr")
        conn.execute("ALTER TABLE ledger DROP COLUMN repo")
        conn.execute("ALTER TABLE ledger DROP COLUMN pr_number")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_outcome")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
        # Migrations 16 to 19 go too, for the same reason.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("ALTER TABLE queue DROP COLUMN command")
        conn.execute("ALTER TABLE runs DROP COLUMN description")
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
        # Migrations 11 to 13 go too, for the reason above: a rewound
        # version replays every later migration, and an ALTER cannot
        # run twice.
        conn.execute("DROP INDEX ledger_by_pr")
        conn.execute("ALTER TABLE ledger DROP COLUMN repo")
        conn.execute("ALTER TABLE ledger DROP COLUMN pr_number")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_outcome")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
        # Migrations 16 to 19 go too, for the same reason.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("ALTER TABLE queue DROP COLUMN command")
        conn.execute("ALTER TABLE runs DROP COLUMN description")
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
        # Migrations 11 to 13 go too, for the reason above: a rewound
        # version replays every later migration, and an ALTER cannot
        # run twice.
        conn.execute("DROP INDEX ledger_by_pr")
        conn.execute("ALTER TABLE ledger DROP COLUMN repo")
        conn.execute("ALTER TABLE ledger DROP COLUMN pr_number")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_outcome")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_attempts")
        conn.execute("ALTER TABLE runs DROP COLUMN publish_failed_at")
        # Migrations 16 to 19 go too, for the same reason.
        conn.execute("ALTER TABLE ledger DROP COLUMN reviewed_since")
        conn.execute("ALTER TABLE runs DROP COLUMN omitted")
        conn.execute("ALTER TABLE runs DROP COLUMN assessment")
        conn.execute("ALTER TABLE queue DROP COLUMN command")
        conn.execute("ALTER TABLE runs DROP COLUMN description")
        conn.execute("PRAGMA user_version = 5")
        conn.execute(
            "INSERT INTO ledger (dedupe_key, owner, actor_id, mode, "
            "reserved_tokens, reserved_at) VALUES ('k', 'w', 1, 'full', 10, 'x')"
        )

    with SqliteStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        with reopened.transaction() as conn:
            assert conn.execute("SELECT stop_reason FROM ledger").fetchone() == (None,)


def test_a_statement_carrying_a_semicolon_is_applied_whole(tmp_path):
    """Migrations are statements, not a script split on ``;`` at runtime.

    A ``CHECK`` listing a value with a semicolon in it -- or a trigger body,
    or a comment -- would be cut in half by such a split, half-applying the
    migration inside its own transaction and leaving the operator to read a
    syntax error about a fragment nobody wrote.
    """
    path = tmp_path / "state.db"
    SqliteStore(path).close()

    semicolons = (
        *store_module._MIGRATIONS,
        ("CREATE TABLE odd (sep TEXT NOT NULL CHECK (sep IN ('a;b', 'c')))",),
    )
    with (
        mock.patch.object(store_module, "_MIGRATIONS", semicolons),
        SqliteStore(path) as store,
    ):
        assert store.schema_version == len(semicolons)
        with store.transaction() as conn:
            conn.execute("INSERT INTO odd (sep) VALUES ('a;b')")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO odd (sep) VALUES ('nope')")


def test_a_failed_migration_leaves_the_version_behind(tmp_path):
    """The script and its version bump commit together, or neither does.

    Without that, a crash between them would leave a half-applied schema at
    a version claiming it was finished -- and ``ALTER TABLE ADD COLUMN``,
    unlike every ``CREATE`` above it, cannot be written to tolerate a replay.
    """
    path = tmp_path / "state.db"
    SqliteStore(path).close()

    broken = (*store_module._MIGRATIONS, ("CREATE TABLE ok (a INTEGER)", "NOT SQL"))
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
