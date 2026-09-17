"""SqliteStore: what has to survive a restart, and what must never go back."""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.store import SqliteStore

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
