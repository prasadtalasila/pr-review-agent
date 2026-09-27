"""The two package-layout trees, checked against the package.

`DEVELOPER.md` and `AGENTS.md` each draw the tree of `src/pr_review_agent`,
and both drifted for several releases: between them they omitted
`publisher.py`, `runs.py`, `numbering.py`, `logs.py`, `cli/cmd_service.py`,
the four newer `engine/` modules, `workspace/exclusions.py` and the two
systemd unit templates. The layout is the first thing a new reader trusts,
and one that omits the module doing the spending is worse than none -- so
this is the same idea as ``test_readme_links.py``: a document that makes a
checkable claim about the repository gets it checked.

The assertion is one-directional on purpose. Every shipped module must be
named; a tree is free to name a directory, a glob or a prose line the walk
knows nothing about, because that is how a tree stays readable.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "pr_review_agent"

DEVELOPER = (ROOT / "DEVELOPER.md").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

#: Skipped: an `__init__.py` is the directory the tree already draws, and
#: `cli/__init__.py` -- the only one carrying code rather than re-exports --
#: is named in both trees anyway. Requiring five more lines that say
#: "this package is a package" would make the trees worse, not truer.
NOT_NAMED_INDIVIDUALLY = {"__init__.py"}


def shipped_modules() -> list[str]:
    """Every module and unit file under the package, as `parent/name`."""
    paths = sorted(PACKAGE.rglob("*.py")) + sorted(PACKAGE.rglob("*.service"))
    return [
        p.name if p.parent == PACKAGE else f"{p.parent.name}/{p.name}"
        for p in paths
        if p.name not in NOT_NAMED_INDIVIDUALLY
    ]


def missing_from(document: str) -> list[str]:
    """Which shipped modules the document never names.

    Matched on the bare file name, because the trees indent rather than
    repeat the directory: `cli/cmd_service.py` appears as `cmd_service.py`.
    Two modules sharing a name -- `engine/models.py` and
    `triggers/models.py` -- are therefore satisfied by one mention. That is
    the price of matching a hand-drawn tree, and this test is here to catch
    a module nobody documented at all, not to police which line it is on.
    """
    return [
        module
        for module in shipped_modules()
        if module.rsplit("/", 1)[-1] not in document
    ]


def test_the_package_has_modules_at_all():
    """Guards the two tests below against an empty walk silently passing."""
    assert len(shipped_modules()) > 30


def test_developer_md_names_every_shipped_module():
    assert missing_from(DEVELOPER) == []


def test_agents_md_names_every_shipped_module():
    assert missing_from(AGENTS) == []


def test_both_trees_ship_the_systemd_units():
    """The units are data in the wheel, so nothing else would notice them."""
    for document in (DEVELOPER, AGENTS):
        assert "pr-review-agent.service" in document
        assert "pr-review-agent@.service" in document


def test_the_walk_would_notice_an_unnamed_module():
    """The tests above pass; this is what says they can also fail."""
    assert missing_from("a document naming nothing") == shipped_modules()
