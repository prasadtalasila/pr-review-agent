"""Standard-library shims for the Python versions this package supports.

The package supports 3.10 through 3.14. Anything here exists solely because a
name is missing on the oldest of those; each shim is deleted, not rewritten,
once the floor rises past the version that needs it.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - exercised on 3.10 only
    from enum import Enum

    class StrEnum(str, Enum):
        """`enum.StrEnum` for Python 3.10.

        A plain ``(str, Enum)`` mixin is not equivalent: on 3.10 it inherits
        ``Enum.__str__``, so ``str(member)`` yields ``"Endpoint.OPEN_PULLS"``
        rather than the member's value. Anything that interpolates a member
        into a string -- a log line, a dict key written to disk -- would then
        change meaning with the interpreter version. Delegating ``__str__``
        and ``__format__`` to ``str`` restores the 3.11 behaviour.
        """

        __str__ = str.__str__
        __format__ = str.__format__


__all__ = ["StrEnum"]
