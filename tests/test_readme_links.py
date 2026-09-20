"""The README is the PyPI project description, so its links must be absolute.

A relative link is correct on GitHub and broken on PyPI, where it resolves
against ``pypi.org`` -- which is how sixteen releases shipped a documentation
table where no entry led anywhere. Pinning the links to the released tag is
what keeps a project page describing the version it belongs to, and this
module is what stops either property rotting silently.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

# ``[text](target)`` -- enough for a hand-written table; reference-style links
# and images are not used in this README.
LINKS = re.findall(r"\[[^\]]+\]\(([^)]+)\)", README)

BLOB = "https://github.com/prasadtalasila/pr-review-agent/blob/"


def project_version() -> str:
    line = re.search(
        r"^version = \"([^\"]+)\"", (ROOT / "pyproject.toml").read_text(), re.M
    )
    assert line, "pyproject.toml has no version"
    return line.group(1)


def test_the_readme_has_links_at_all():
    """Guards the two tests below against an empty match silently passing."""
    assert len(LINKS) > 10


def test_every_readme_link_is_absolute():
    relative = [target for target in LINKS if not target.startswith("https://")]
    assert relative == []


def test_every_repository_link_pins_the_released_version():
    tag = f"v{project_version()}"
    stale = [
        target
        for target in LINKS
        if target.startswith(BLOB) and not target.startswith(f"{BLOB}{tag}/")
    ]
    assert stale == []
