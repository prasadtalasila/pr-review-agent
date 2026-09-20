"""The README's links, and the rewrite that makes them work on PyPI.

Two properties, and neither one alone is enough. The source README's
relative targets have to name files that exist, or the rewrite produces
absolute URLs that 404 just as the relative ones did; and the rewrite has to
leave nothing relative behind, or PyPI resolves what is left against
``pypi.org``.
"""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "pypi_readme", ROOT / "scripts" / "pypi_readme.py"
)
assert _spec and _spec.loader
pypi_readme = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pypi_readme)

README = (ROOT / "README.md").read_text(encoding="utf-8")
LINKS = re.findall(r"\[[^\]]+\]\(([^)]+)\)", README)

REPOSITORY = "https://github.com/prasadtalasila/pr-review-agent"


def rendered(version: str = "9.9.9") -> str:
    return pypi_readme.render(README, REPOSITORY, version)


def test_the_readme_has_links_at_all():
    """Guards the tests below against an empty match silently passing."""
    assert len(LINKS) > 10


def test_every_relative_link_names_a_file_that_exists():
    missing = [
        target
        for target in LINKS
        if not target.startswith(("https://", "#")) and not (ROOT / target).exists()
    ]
    assert missing == []


def test_the_rendered_readme_keeps_nothing_relative():
    targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", rendered())
    assert [t for t in targets if not t.startswith(("https://", "#"))] == []


def test_the_rendered_links_are_pinned_to_the_version_being_released():
    assert f"]({REPOSITORY}/blob/v9.9.9/docs/CONFIG.md)" in rendered()


def test_a_link_that_is_already_absolute_is_left_alone():
    assert (
        pypi_readme.render(
            "[releases](https://example.test/releases)", REPOSITORY, "9.9.9"
        )
        == "[releases](https://example.test/releases)"
    )


def test_an_in_page_anchor_is_left_alone():
    assert pypi_readme.render("[top](#top)", REPOSITORY, "9.9.9") == "[top](#top)"


def test_the_version_and_repository_come_from_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert pypi_readme.pyproject_field(pyproject, "Repository") == REPOSITORY
    assert re.fullmatch(
        r"\d+\.\d+\.\d+", pypi_readme.pyproject_field(pyproject, "version")
    )
