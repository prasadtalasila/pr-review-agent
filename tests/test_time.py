"""The shared clock: the storage format, and the interruptible wait."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent._time import now, parse, stamp, to_utc, wait_until


def test_now_is_aware_and_utc():
    assert now().tzinfo is timezone.utc


def test_a_naive_datetime_is_refused_and_the_error_names_the_field():
    with pytest.raises(ValueError, match="enqueued_at must be timezone-aware"):
        stamp(datetime(2026, 9, 17, 7, 11), "enqueued_at")


def test_another_zone_is_converted_rather_than_rejected():
    kolkata = timezone(timedelta(hours=5, minutes=30))
    assert to_utc(datetime(2026, 9, 17, 12, 41, tzinfo=kolkata), "at") == datetime(
        2026, 9, 17, 7, 11, tzinfo=timezone.utc
    )


def test_a_stamp_round_trips_through_parse():
    at = now()
    assert parse(stamp(at)) == at


def test_parse_accepts_the_z_suffix_github_writes():
    assert parse("2026-09-17T07:11:00Z") == datetime(
        2026, 9, 17, 7, 11, tzinfo=timezone.utc
    )


def test_stamps_sort_lexicographically_in_the_order_the_instants_occur():
    """The invariant the lease and window predicates are plain SQL because of.

    The whole second is the interesting case: ``+`` sorts before ``.``, so it
    must still precede the same second carrying a fraction.
    """
    whole = datetime(2026, 9, 17, 7, 11, tzinfo=timezone.utc)
    instants = [
        whole - timedelta(days=1),
        whole,
        whole + timedelta(microseconds=1),
        whole + timedelta(seconds=1),
        whole + timedelta(hours=1),
    ]
    stamps = [stamp(at) for at in instants]
    assert stamps == sorted(stamps)


async def test_a_wait_ends_early_when_the_stop_event_is_set():
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(wait_until(stop, 30.0), timeout=5)


async def test_a_wait_that_times_out_returns_rather_than_raising():
    await wait_until(asyncio.Event(), 0.01)
