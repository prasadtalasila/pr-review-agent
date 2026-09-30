"""Patterns to pathspecs: the one mechanism both uses of an exclusion share."""

import sys

import pytest

from pr_review_agent.config import DEFAULT_EXCLUDED_PATHS
from pr_review_agent.workspace.exclusions import pathspec
from pr_review_agent.workspace.gitcmd import run_git


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


def test_every_built_in_pattern_is_a_plain_glob():
    """The same rule ``budget.excluded_paths`` is held to: the pathspec magic
    is the agent's to supply, so a copied list may not open its own."""
    for pattern in DEFAULT_EXCLUDED_PATHS:
        assert isinstance(pattern, str) and pattern.strip()
        assert not pattern.startswith(":")


def test_the_built_ins_hold_no_duplicates():
    """pr-agent's groups are copied verbatim; the three local patterns they
    already covered were removed rather than listed twice."""
    assert len(set(DEFAULT_EXCLUDED_PATHS)) == len(DEFAULT_EXCLUDED_PATHS)


#: One pattern per upstream generator group, a path it must exclude, and a
#: sibling source file it must leave alone.
GENERATORS = [
    ("**/*.pb.go", "api/v1/user.pb.go", "api/v1/user.go"),
    ("**/__generated__/**", "client/__generated__/api.ts", "client/api.ts"),
    ("**/swagger.json", "docs/swagger.json", "docs/openapi.md"),
    ("**/*.graphql.ts", "src/types.graphql.ts", "src/types.ts"),
    ("**/*_grpc.py", "svc/user_grpc.py", "svc/user.py"),
    ("**/*_gen.go", "pkg/models_gen.go", "pkg/models.go"),
]


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX git")
@pytest.mark.parametrize(("pattern", "generated", "sibling"), GENERATORS)
async def test_each_generator_group_excludes_its_output_and_keeps_the_source(
    tmp_path, pattern, generated, sibling
):
    """Proven under git rather than ``fnmatch``: ``**`` means "at any depth"
    only with the ``glob`` magic, which is what the pathspec supplies."""
    assert pattern in DEFAULT_EXCLUDED_PATHS
    for relative in (generated, sibling):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    await run_git("init", "-q", cwd=tmp_path)
    await run_git("add", "--all", cwd=tmp_path)
    listed = await run_git("ls-files", *pathspec((pattern,)), cwd=tmp_path)
    assert listed.split() == [sibling]
