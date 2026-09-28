"""The ledger's one open reservation per trigger, and the contention it meets.

The partial index is what makes a settle unambiguous, and `is_contention`
is what keeps another daemon's write lock from reading as a fault.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from pr_review_agent.store import SCHEMA_VERSION, SqliteStore, is_contention

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
RESERVED_AT = "2026-09-27T12:00:00+00:00"


def _reserve(conn, key: str, tokens: int) -> None:
    """One open ledger row, written the way ``Governor.admit`` writes it."""
    conn.execute(
        "INSERT INTO ledger (dedupe_key, owner, actor_id, mode, "
        "reserved_tokens, reserved_at) "
        "VALUES (:key, 'w', 1, 'full', :tokens, :at)",
        {"key": key, "tokens": tokens, "at": RESERVED_AT},
    )


def test_one_trigger_can_hold_only_one_open_reservation(tmp_path):
    """What identifies a reservation while it is open, held by the schema.

    ``settle`` matches on the key and the owner, so two open rows for one
    key made one settle update both -- and a worker that could not settle
    discarded a review it had already paid for.
    """
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        _reserve(conn, "k", 10)
        with pytest.raises(sqlite3.IntegrityError):
            _reserve(conn, "k", 10)


def test_a_settled_reservation_leaves_the_key_free_again(tmp_path):
    """The index is on the *open* rows only: a key is reviewed many times."""
    with SqliteStore(tmp_path / "state.db") as store, store.transaction() as conn:
        _reserve(conn, "k", 10)
        conn.execute("UPDATE ledger SET settled_at = :at", {"at": RESERVED_AT})
        _reserve(conn, "k", 10)
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone() == (1 + 1,)


def test_an_existing_database_settles_its_duplicate_open_reservations(tmp_path):
    """A v14 store already holding duplicates adopts the index anyway.

    The index cannot be created over them, and a store that will not migrate
    is a daemon that will not start -- so the migration closes the older rows
    first. They settle at their full reservation because what a worker that
    never settled really spent is unknowable, and the newest row survives
    open because a live worker may still be the one holding it.
    """
    path = tmp_path / "state.db"
    with SqliteStore(path) as store, store.transaction() as conn:
        conn.execute("DROP INDEX ledger_open")
        for tokens in (10, 20, 30):
            _reserve(conn, "k", tokens)
        _reserve(conn, "untouched", 40)
        conn.execute("PRAGMA user_version = 14")

    with SqliteStore(path) as reopened, reopened.transaction() as conn:
        assert reopened.schema_version == SCHEMA_VERSION
        assert conn.execute(
            "SELECT reserved_tokens, used_tokens, usage_confidence, "
            "stop_reason, settled_at FROM ledger WHERE dedupe_key = 'k' "
            "ORDER BY id"
        ).fetchall() == [
            (10, 10, "unavailable", "lost", RESERVED_AT),
            (20, 20, "unavailable", "lost", RESERVED_AT),
            # The newest is left alone: it is the one still owed a settle.
            (30, None, None, None, None),
        ]
        # A key with one open row is not a duplicate and is not touched.
        assert conn.execute(
            "SELECT used_tokens FROM ledger WHERE dedupe_key = 'untouched'"
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("ledger_open",),
        ).fetchone() == (1,)


def test_contention_is_told_apart_from_a_real_fault():
    # The daemon skips a cycle for the first and crashes for the second, so
    # the message is load-bearing: `busy_timeout` reports a wait it lost as
    # an ordinary OperationalError.
    assert is_contention(sqlite3.OperationalError("database is locked"))
    assert is_contention(sqlite3.OperationalError("database table is locked"))
    assert not is_contention(sqlite3.OperationalError("no such table: queue"))
    assert not is_contention(
        sqlite3.OperationalError("attempt to write a readonly database")
    )


def test_a_held_write_lock_really_does_raise_what_is_matched(tmp_path):
    # Not a paraphrase of SQLite's wording: the real driver, a real held
    # lock, and a timeout short enough to lose it.
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        holder = sqlite3.connect(str(path), isolation_level=None)
        holder.execute("PRAGMA busy_timeout=0")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            store._conn.execute("PRAGMA busy_timeout=0")  # noqa: SLF001
            with pytest.raises(sqlite3.OperationalError) as caught:
                store.advance_watermark("pulls", NOON)
            assert is_contention(caught.value)
        finally:
            holder.close()
