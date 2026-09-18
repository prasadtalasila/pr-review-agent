"""Turn configured path patterns into git pathspec arguments.

BUDGET.md layer 2: lockfiles, vendored trees, generated code and minified
bundles dominate a diff's size while being close to worthless to review, so
excluding them is the largest saving available for zero tokens.

**One mechanism serves both places an exclusion has to apply.** The same
argument list goes to ``git diff --numstat``, which the size gate counts, and
to the ``git diff`` that produces ``Checkout.diff``, which is the only diff
in the system and therefore exactly what a review engine is shown. A change
that hides a path from one cannot leave it visible to the other.

The alternative -- filtering the diff text after the fact -- would have meant
two implementations of "is this path excluded", one of them a parser for
git's own output.
"""

from __future__ import annotations

#: Applied per pattern rather than to the pathspec as a whole. ``glob`` is
#: what makes ``**/`` mean "at any depth": without it ``*`` does not cross a
#: ``/``, and ``**/vendor/**`` would match nothing.
_MAGIC = ":(exclude,glob)"

#: A pathspec of exclusions alone matches nothing, so the tree itself has to
#: be listed first for there to be anything to subtract from.
_EVERYTHING = "."


def pathspec(patterns: tuple[str, ...]) -> list[str]:
    """The ``--`` and pathspec arguments excluding ``patterns``.

    Empty for an empty list -- not ``["--", "."]`` -- so an operator who
    excludes nothing gets the plain command, with no pathspec to reason
    about when reading a log.
    """
    if not patterns:
        return []
    return ["--", _EVERYTHING, *(f"{_MAGIC}{pattern}" for pattern in patterns)]
