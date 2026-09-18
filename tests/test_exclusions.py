"""Patterns to pathspecs: the one mechanism both uses of an exclusion share."""

from pr_review_agent.config import DEFAULT_EXCLUDED_PATHS
from pr_review_agent.workspace.exclusions import pathspec


def test_each_pattern_becomes_an_excluding_pathspec():
    assert pathspec(("**/vendor/**", "*.min.js")) == [
        "--",
        ".",
        ":(exclude,glob)**/vendor/**",
        ":(exclude,glob)*.min.js",
    ]


def test_the_tree_is_listed_before_the_exclusions():
    """A pathspec of exclusions alone matches nothing at all.

    Without a positive term there is nothing to subtract from, and the diff
    would come back empty -- which would look exactly like a pull request
    with no reviewable content.
    """
    assert pathspec(("*.lock",))[:2] == ["--", "."]


def test_excluding_nothing_produces_no_pathspec_at_all():
    """Not ``["--", "."]``: an operator who excludes nothing gets the plain
    command, with no pathspec to reason about when reading a log."""
    assert pathspec(()) == []


def test_glob_magic_is_present_on_every_pattern():
    """``**/`` only means "at any depth" with ``glob``; plain ``*`` will not
    cross a ``/``, and ``**/vendor/**`` would then match nothing."""
    assert all(
        argument.startswith(":(exclude,glob)")
        for argument in pathspec(DEFAULT_EXCLUDED_PATHS)[2:]
    )


def test_the_defaults_cover_the_four_categories():
    """BUDGET.md layer 2 names lockfiles, vendored, generated and minified."""
    for pattern in (
        "**/package-lock.json",
        "**/poetry.lock",
        "**/vendor/**",
        "**/node_modules/**",
        "**/*_pb2.py",
        "**/*.min.js",
    ):
        assert pattern in DEFAULT_EXCLUDED_PATHS
