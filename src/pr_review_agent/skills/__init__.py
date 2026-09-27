"""The review skill, shipped in the wheel because two readers need it.

An interactive Claude Code session loads ``review-report/SKILL.md`` and reads
its references on demand. The daemon's own reviewer never does:
``engine/claude.py`` passes ``--setting-sources ""``, ``--restricted`` and
``--disable-slash-commands``, which between them mean no settings file,
plugin or skill is discovered. That is not an oversight to work around. A
reviewer that loads configuration from the tree it is reviewing takes its
instructions from whoever opened the pull request, and three of the four
injection defences in ``docs/DESIGN.md`` are exactly those flags.

So the engine gets the same text by a route that widens nothing:
``engine/prompt.py`` reads ``references/finding-contract.md`` from here and
splices it into the prompt. One source, two deliveries. Before this module
the text existed twice -- once as ``REVIEW_INSTRUCTIONS`` and once as
``docs/reporting/review-prompt.md`` -- and the constant's own comment said
paraphrasing it would let the two drift silently.

Addressed through ``importlib.resources`` rather than ``__file__`` for the
same reason the systemd units are: an installed wheel is the case that has to
work, and for thirteen releases the data files this package needs were not in
it at all. The ``include`` entries in ``pyproject.toml`` are what put them
there, and ``test_skill.py`` is what notices when they stop.
"""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

#: The skill's directory name, which is also the skill's name to Claude Code:
#: the two have to agree, because a skill is resolved by the directory it
#: sits in and named by the ``name:`` in its front matter.
SKILL_NAME = "review-report"


def root():
    """The packaged skill directory, as a ``Traversable``.

    Not a ``Path``: inside a zip-imported wheel there is no directory on
    disk, and returning something that only sometimes has a filesystem path
    is how that case gets discovered in production rather than here.
    """
    return files(__name__).joinpath(SKILL_NAME)


def reference(name: str) -> str:
    """One reference file's text, with the trailing newline stripped.

    Stripped because the caller splicing this into a prompt is assembling
    paragraphs, not concatenating files, and a stray blank line is a
    difference a test comparing prompts would have to know about.
    """
    reference_file = root().joinpath("references").joinpath(name)
    return reference_file.read_text(encoding="utf-8").rstrip("\n")


def install(destination: Path, *, force: bool = False) -> Path:
    """Copy the skill into ``destination``, returning where it landed.

    A file writer and nothing else, in the shape ``service install`` already
    uses: it places files and leaves the decision to *use* them to whoever
    ran it. ``destination`` is a skills directory -- ``~/.claude/skills`` for
    every project, ``<repo>/.claude/skills`` for one -- and the skill lands
    in a ``review-report`` child of it.

    ``force`` overwrites, but only a directory this function could have
    written: an existing ``review-report`` with no ``SKILL.md`` in it is
    somebody else's, and removing it because the name collided would be a
    destructive act taken on a guess.
    """
    target = destination / SKILL_NAME
    if target.exists():
        if not force:
            raise FileExistsError(target)
        if not (target / "SKILL.md").is_file():
            raise FileExistsError(target)
        shutil.rmtree(target)
    _copy(root(), target)
    return target


def _copy(source, target: Path) -> None:
    """Recursive copy out of a ``Traversable``, which ``shutil`` cannot do.

    ``__pycache__`` is skipped: pip byte-compiles the scripts at install
    time, and a skill directory carrying three ``.pyc`` files invites the
    reader to wonder which of them matters.
    """
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name == "__pycache__":
            continue
        child = target / entry.name
        if entry.is_dir():
            _copy(entry, child)
        else:
            child.write_bytes(entry.read_bytes())
