"""Adaptive poll interval: fast while a repo is active, slow while it is not.

Every 304 across all three endpoints decays the interval towards the
ceiling; any single 200 (something changed) snaps straight back to the
minimum. Snapping to the floor rather than easing down keeps detection
latency low right when a repository just became active, which is when it
matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_SECONDS = 10
MAX_SECONDS = 600
DECAY_FACTOR = 2.0


@dataclass
class AdaptiveInterval:
    """Tracks the current poll delay for one repository."""

    min_seconds: int = MIN_SECONDS
    max_seconds: int = MAX_SECONDS
    decay_factor: float = DECAY_FACTOR
    _seconds: float = field(init=False)

    def __post_init__(self) -> None:
        self._seconds = float(self.min_seconds)

    @property
    def seconds(self) -> int:
        """The current delay, in whole seconds."""
        return int(self._seconds)

    def record(self, *, changed: bool) -> None:
        """Update the interval after one poll cycle."""
        if changed:
            self._seconds = float(self.min_seconds)
        else:
            self._seconds = min(self._seconds * self.decay_factor, self.max_seconds)

    def force_ceiling(self) -> None:
        """Snap straight to the ceiling, bypassing the normal decay curve.

        Used when GitHub's rate-limit budget is nearly exhausted: hold the
        slowest safe interval until the window resets, rather than let a
        burst of activity decay the interval back down to the floor and
        spend what's left of the budget.
        """
        self._seconds = float(self.max_seconds)
