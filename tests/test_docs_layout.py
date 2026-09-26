"""The two package-layout trees, against the package they describe.

Both trees drifted for several releases: ``DEVELOPER.md`` omitted ten modules
and ``AGENTS.md`` four, so a reader of either believed the package was smaller
than it is. Neither tree is generated -- they carry hand-written notes that are
the reason to have them -- so what is checked here is membership in both
directions: nothing in the package is missing from a tree, and nothing in a
tree has been deleted from the package.

The two trees are at different resolutions on purpose. ``DEVELOPER.md``
enumerates every module; ``AGENTS.md`` names the top-level modules and
summarises each subpackage in one line. The assertions below follow that.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "pr_review_agent"

DEVELOPER = (ROOT / "DEVELOPER.md").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def tree(document: str) -> str:
    """The fenced ``text`` block that opens with the package directory."""
    for block in re.findall(r"```text\n(.*?)```", document, re.DOTALL):
        if block.lstrip().startswith("src/pr_review_agent/"):
            return block
    raise AssertionError("no package layout tree in this document")


DEVELOPER_TREE = tree(DEVELOPER)
AGENTS_TREE = tree(AGENTS)

#: Every module a reader should be able to find, ``__init__.py`` aside: an
#: ``__init__`` is the package, which the tree names as a directory.
MODULES = sorted(
    path.relative_to(PACKAGE).as_posix()
    for path in PACKAGE.rglob("*.py")
    if path.name != "__init__.py"
)
#: The top level only -- what AGENTS.md lists module by module.
TOP_LEVEL = sorted(name for name in MODULES if "/" not in name)
#: The subpackages, which AGENTS.md summarises rather than expands.
SUBPACKAGES = sorted(
    path.name
    for path in PACKAGE.iterdir()
    if path.is_dir() and path.name != "__pycache__"
)


def test_the_package_has_modules_and_subpackages_at_all():
    """Guards the tests below against an empty walk silently passing."""
    assert len(MODULES) > 20
    assert len(TOP_LEVEL) > 8
    assert len(SUBPACKAGES) > 3


def test_developer_md_names_every_module():
    missing = [name for name in MODULES if Path(name).name not in DEVELOPER_TREE]
    assert missing == []


def test_agents_md_names_every_top_level_module():
    missing = [name for name in TOP_LEVEL if name not in AGENTS_TREE]
    assert missing == []


def test_agents_md_names_every_subpackage():
    missing = [name for name in SUBPACKAGES if f"{name}/" not in AGENTS_TREE]
    assert missing == []


def test_developer_md_names_every_subpackage():
    missing = [name for name in SUBPACKAGES if f"{name}/" not in DEVELOPER_TREE]
    assert missing == []


def test_neither_tree_names_a_module_that_is_gone():
    """The other direction: a deleted module left behind in a tree."""
    known = {Path(name).name for name in MODULES} | {"__init__.py"}
    for label, block in (("DEVELOPER.md", DEVELOPER_TREE), ("AGENTS.md", AGENTS_TREE)):
        named = set(re.findall(r"\b([a-z_][a-z0-9_]*\.py)\b", block))
        assert named <= known, f"{label} names a module that does not exist"


def test_the_templates_directory_is_described_as_shipped_data():
    """`templates/` is data, not code, so rglob('*.py') would never see it."""
    shipped = sorted(
        path.name
        for path in (PACKAGE / "templates").iterdir()
        if path.suffix in {".yaml", ".service"}
    )
    assert shipped, "the templates directory is empty"
    for block in (DEVELOPER_TREE, AGENTS_TREE):
        assert "templates/" in block


# -- the bounded sets a document enumerates ------------------------------
#
# `STORAGE.md` listed seven stop reasons while the enum had nine, for two
# releases. The list is the operator's reference for `GROUP BY stop_reason`,
# so a missing value is a reason they will not think to look for.


def test_storage_md_lists_every_stop_reason():
    from pr_review_agent.budget import StopReason

    storage = (ROOT / "docs" / "STORAGE.md").read_text(encoding="utf-8")
    section = storage.split("`stop_reason` is why the run ended", 1)[1]
    missing = [reason for reason in StopReason if f"`{reason}`" not in section[:1500]]
    assert missing == []


def test_storage_md_counts_the_stop_reasons_it_lists():
    """The number in the prose, against the enum it describes."""
    from pr_review_agent.budget import StopReason

    storage = (ROOT / "docs" / "STORAGE.md").read_text(encoding="utf-8")
    spelled = {
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
        11: "eleven",
        12: "twelve",
    }
    assert f"One of {spelled[len(list(StopReason))]} values" in storage
