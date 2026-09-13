"""Adaptive interval: fast while active, decaying while quiet."""

from pr_review_agent.poller.interval import AdaptiveInterval


def test_starts_at_the_floor():
    assert AdaptiveInterval(min_seconds=10, max_seconds=600).seconds == 10


def test_quiet_cycles_decay_towards_the_ceiling():
    interval = AdaptiveInterval(min_seconds=10, max_seconds=600, decay_factor=2.0)
    seen = [interval.seconds]
    for _ in range(6):
        interval.record(changed=False)
        seen.append(interval.seconds)
    assert seen == [10, 20, 40, 80, 160, 320, 600]  # last step clamps to the ceiling


def test_decay_never_exceeds_the_ceiling():
    interval = AdaptiveInterval(min_seconds=10, max_seconds=600, decay_factor=2.0)
    for _ in range(20):
        interval.record(changed=False)
    assert interval.seconds == 600


def test_a_single_change_snaps_back_to_the_floor():
    interval = AdaptiveInterval(min_seconds=10, max_seconds=600, decay_factor=2.0)
    for _ in range(5):
        interval.record(changed=False)
    assert interval.seconds > 10
    interval.record(changed=True)
    assert interval.seconds == 10


def test_repeated_activity_holds_at_the_floor():
    interval = AdaptiveInterval(min_seconds=10, max_seconds=600)
    for _ in range(5):
        interval.record(changed=True)
    assert interval.seconds == 10


def test_force_ceiling_snaps_straight_to_the_max():
    interval = AdaptiveInterval(min_seconds=10, max_seconds=600)
    interval.force_ceiling()
    assert interval.seconds == 600
