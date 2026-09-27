"""The ``engine`` section: which tool reviews, and under what clock."""

from __future__ import annotations

from dataclasses import dataclass

from ..queue import DEFAULT_LEASE
from ._sections import ConfigError

#: The CLI an adapter runs when the operator does not name one.
DEFAULT_ENGINE_BINARY = "claude"


@dataclass(frozen=True)
class EngineConfig:
    """Which coding agent reviews, and the rails around one invocation.

    ``model`` and ``expected_version`` have no defaults for the same reason
    the plan token counts have none: a default model is a cost nobody chose,
    and a default version pin is a claim about output nobody checked.

    ``standards_paths`` are read from the repository under review **at the
    merge base**, never at the pull request head, so opening a pull request
    cannot rewrite the reviewer's instructions.
    """

    model: str
    expected_version: str
    timeout_seconds: float
    binary: str = DEFAULT_ENGINE_BINARY
    standards_paths: tuple[str, ...] = ()

    @classmethod
    def parse(cls, data: dict) -> EngineConfig:
        """Validate the ``engine`` section."""
        return cls(
            model=_text(data, "model"),
            expected_version=_text(data, "expected_version"),
            timeout_seconds=_timeout(data),
            binary=(
                _text(data, "binary") if "binary" in data else DEFAULT_ENGINE_BINARY
            ),
            standards_paths=_standards_paths(data),
        )


def _text(data: dict, key: str) -> str:
    """Read a required non-empty string from the ``engine`` section."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"engine.{key} must be a non-empty string")
    return value


def _timeout(data: dict) -> float:
    """Read the wall clock one review may not outlive.

    Bounded above by the queue lease. ``queue.py`` calls ``DEFAULT_LEASE``
    "comfortably above the per-run wall-clock ceiling the budget governor
    enforces", and its module docstring goes further: a lease carries an
    expiry rather than a heartbeat *because* a run has a ceiling, so a
    renewal "would be machinery for a case that cannot arise". An operator
    who sets an hour makes that case arise -- the lease lapses under a live
    worker, a second worker claims the same pull request and reserves against
    the same windows, and the first worker's ``settle`` returns ``False`` and
    discards a review that was paid for. Until now the invariant was a
    comment.

    Strictly below, not equal: at exactly the lease the two expire together
    and which one wins is a scheduling race.
    """
    value = data.get("timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError("engine.timeout_seconds must be a positive number")
    lease = DEFAULT_LEASE.total_seconds()
    if value >= lease:
        raise ConfigError(
            f"engine.timeout_seconds ({value:g}) must be below the queue lease "
            f"({lease:g}s); a run that outlives its lease loses it mid-review"
        )
    return float(value)


def _standards_paths(data: dict) -> tuple[str, ...]:
    """Read the optional list of standards files."""
    value = data.get("standards_paths", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError("engine.standards_paths must be a list of repository paths")
    return tuple(value)
