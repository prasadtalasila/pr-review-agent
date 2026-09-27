"""What every section of the file has in common.

The error the loader raises, and the two rules that apply to each section
regardless of what it holds: it must be a mapping, and it may carry no key
its dataclass cannot.
"""

from __future__ import annotations

from dataclasses import fields


class ConfigError(ValueError):
    """Raised when the configuration file is unusable."""


def _keys(schema: type) -> set[str]:
    """The YAML keys a section accepts: exactly its dataclass fields.

    Spelling the set out beside the dataclass made the two drift in either
    direction, and both directions are silent. A field added without its key
    is rejected as unknown; a key kept after its field is removed is accepted
    and ignored -- which is precisely the failure unknown-key rejection is
    documented in ``docs/CONFIG.md`` to prevent. Deriving it means a section
    accepts what it can hold, by construction.

    A YAML key that has to differ from its field name therefore cannot be
    added silently: it would need an alias here, deliberately.
    """
    return {field.name for field in fields(schema)}


def _section(data: dict, name: str, schema: type) -> dict:
    """Return section ``name``, rejecting keys ``schema`` cannot hold."""
    value = data.get(name)
    if value is None:
        raise ConfigError(f"missing required section: {name!r}")
    if not isinstance(value, dict):
        raise ConfigError(f"section {name!r} must be a mapping")
    unknown = sorted(set(value) - _keys(schema))
    if unknown:
        raise ConfigError(f"unknown keys in {name!r}: {unknown}")
    return value
