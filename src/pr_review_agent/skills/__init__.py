"""The review and description skills, shipped in the wheel because two
readers need them.

An interactive Claude Code session loads ``review-report/SKILL.md`` and reads
its references on demand. The daemon's own reviewer never does:
``engine/claude.py`` passes ``--setting-sources ""``, ``--restricted`` and
``--disable-slash-commands``, which between them mean no settings file,
plugin or skill is discovered. That is not an oversight to work around. A
reviewer that loads configuration from the tree it is reviewing takes its
instructions from whoever opened the pull request, and three of the four
injection defences in ``docs/DESIGN.md`` are exactly those flags.

So the engine gets the same text by a route that widens nothing:
``engine/prompt.py`` reads ``references/finding-contract.md`` and
``references/false-positives.md`` from here and splices them into the
prompt. One source, two deliveries. Before this module
the text existed twice -- once as ``REVIEW_INSTRUCTIONS`` and once as
``docs/reporting/review-prompt.md`` -- and the constant's own comment said
paraphrasing it would let the two drift silently.

``pr-description`` is the second skill and follows the same rule: the
daemon's ``@claude describe`` prompt reads its
``references/description-contract.md`` from here (``engine/describe.py``).

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

#: The skill behind ``@claude describe``, and the one a person uses to write
#: a pull request description by hand.
DESCRIBE_SKILL = "pr-description"

#: Every skill ``skill install`` places, in the order it places them.
SKILLS = (SKILL_NAME, DESCRIBE_SKILL)

#: The distribution package these modules live in, which is also the
#: directory name the vendored copy has to take: the scripts import
#: ``pr_review_agent.report`` whichever of the two answers.
PACKAGE = __name__.rsplit(".", 1)[0]

#: Where ``install`` puts the modules the scripts import, relative to the
#: skill. On ``sys.path`` *after* the interpreter's own entries, so an
#: installed ``pr_review_agent`` always wins: this copy is the fallback for
#: a machine that has the skill and not the package, not a fork of it.
VENDOR_DIR = ("scripts", "_vendor")

#: The whole import closure of ``report.render``, ``numbering.assign`` and
#: the four constants ``check_report.py`` reads -- every one of them poor
#: enough to travel, none of them importing config, sqlite or httpx.
#:
#: This is the list that makes ``skill install`` produce something that
#: works on its own. It is short because :mod:`pr_review_agent.report` and
#: :mod:`pr_review_agent.findings` were split out to keep it short; adding
#: a rich import to any of these files lengthens it without saying so, and
#: ``test_skill.py`` renders the worked example with the real package
#: blocked so that the lengthening fails rather than ships.
VENDORED = (
    "__init__.py",
    "_compat.py",
    "description.py",
    "findings.py",
    "numbering.py",
    "report.py",
    "sanitise.py",
    "triggers/mention.py",
)

#: ``triggers/__init__.py`` is written rather than copied. The real one
#: re-exports the allowlist, the classifier and the payload models, so
#: importing it would pull in the config schema and the HTTP client to
#: reach ``mention``, which needs nothing but ``re``. A package marker is
#: not behaviour, so replacing it is not a second copy of anything.
TRIGGERS_STUB = '''"""Package marker only -- see ``skills.TRIGGERS_STUB``.

``pr_review_agent.triggers`` proper re-exports the trigger pipeline. The
skill needs one module out of this package, ``mention``, and nothing that
the real ``__init__`` imports on the way to it.
"""
'''


def root(skill: str = SKILL_NAME):
    """A packaged skill directory, as a ``Traversable``.

    Not a ``Path``: inside a zip-imported wheel there is no directory on
    disk, and returning something that only sometimes has a filesystem path
    is how that case gets discovered in production rather than here.
    """
    return files(__name__).joinpath(skill)


def reference(name: str, skill: str = SKILL_NAME) -> str:
    """One reference file's text, with the trailing newline stripped.

    Stripped because the caller splicing this into a prompt is assembling
    paragraphs, not concatenating files, and a stray blank line is a
    difference a test comparing prompts would have to know about.
    """
    reference_file = root(skill).joinpath("references").joinpath(name)
    return reference_file.read_text(encoding="utf-8").rstrip("\n")


def install(destination: Path, *, force: bool = False, skill: str = SKILL_NAME) -> Path:
    """Copy one skill into ``destination``, returning where it landed.

    A file writer and nothing else, in the shape ``service install`` already
    uses: it places files and leaves the decision to *use* them to whoever
    ran it. ``destination`` is a skills directory -- ``~/.claude/skills`` for
    every project, ``<repo>/.claude/skills`` for one -- and the skill lands
    in a child named for it. Each skill carries its own vendored copy of the
    package, so either works installed alone.

    ``force`` overwrites, but only a directory this function could have
    written: an existing ``review-report`` with no ``SKILL.md`` in it is
    somebody else's, and removing it because the name collided would be a
    destructive act taken on a guess.
    """
    target = destination / skill
    if target.exists():
        if not force:
            raise FileExistsError(target)
        if not (target / "SKILL.md").is_file():
            raise FileExistsError(target)
        shutil.rmtree(target)
    _copy(root(skill), target)
    _vendor(target.joinpath(*VENDOR_DIR) / PACKAGE)
    return target


def _vendor(target: Path) -> None:
    """Copy ``VENDORED`` into ``target``, which is a ``pr_review_agent`` dir.

    Named for the package rather than something neutral because the scripts
    import ``pr_review_agent.report``, not a private alias: the same import
    line has to resolve whether the package is installed or only copied, or
    the two paths are two code paths and only one of them is tested.
    """
    package = files(PACKAGE)
    for name in VENDORED:
        source = package.joinpath(*name.split("/"))
        destination = target.joinpath(*name.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    (target / "triggers" / "__init__.py").write_text(TRIGGERS_STUB, encoding="utf-8")


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
