"""README.md with every relative link made absolute and pinned to a tag.

PyPI renders README.md as the project description, and a relative target
there resolves against `pypi.org` rather than the repository -- which is how
sixteen releases shipped a documentation table where no entry led anywhere.

The source README keeps its relative links, because those are what work on
GitHub and in the mkdocs site, and because `pyproject.toml`'s `readme` has to
name a file every checkout actually has. The release workflow runs this
script and builds from its output instead, so each published version's page
points at the tag that version was cut from rather than at whatever `main`
says today.

Usage: ``python scripts/pypi_readme.py`` writes ``README.pypi.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def pyproject_field(pyproject: str, name: str) -> str:
    """The value of a top-of-line ``name = "value"`` entry in pyproject.toml."""
    found = re.search(rf'^{name} = "([^"]+)"', pyproject, re.M)
    if not found:
        raise SystemExit(f"pyproject.toml has no {name}")
    return found.group(1)


def render(readme: str, repository: str, version: str) -> str:
    """`readme`, with each relative link absolute and pinned to `v<version>`.

    A target that is already absolute, or that is an in-page anchor, is left
    exactly as it was: the rewrite has nothing to add to either.
    """
    blob = f"{repository}/blob/v{version}/"

    def rewrite(link: re.Match[str]) -> str:
        text, target = link.group(1), link.group(2)
        if target.startswith(("http://", "https://", "#")):
            return link.group(0)
        return f"[{text}]({blob}{target})"

    return LINK.sub(rewrite, readme)


def main() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    rendered = render(
        (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        pyproject_field(pyproject, "Repository"),
        pyproject_field(pyproject, "version"),
    )
    (REPO_ROOT / "README.pypi.md").write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
