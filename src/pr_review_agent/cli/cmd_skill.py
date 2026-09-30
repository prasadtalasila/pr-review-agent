"""``pr-review-agent skill install`` -- put the skills where Claude finds them.

The daemon does not need this verb. Its own reviewer is handed the skill's
text inside the prompt, by ``engine/prompt.py``, precisely because it runs
``claude`` with ``--setting-sources ""``, ``--restricted`` and
``--disable-slash-commands`` and so discovers no skill, plugin or settings
file at all. Relaxing any of those to let it load a directory instead would
trade three argv-level injection defences for a file layout.

This verb is for the other reader: a person, or an interactive Claude Code
session, writing a review or a pull request description by hand. It copies
both packaged skills, ``review-report`` and ``pr-description``, into a
skills directory and prints where it went -- a file writer, in the same
shape as ``service install``, which also declines to take the next step on
the operator's behalf.

``--dir`` defaults to the personal skills directory rather than a
repository's, because the format is not this repository's private business:
the reviews worth writing this way are on other people's code.
"""

from __future__ import annotations

from pathlib import Path

import click

from .. import skills
from ._common import fail

#: Where Claude Code looks for a skill available in every project.
DEFAULT_DIR = Path.home() / ".claude" / "skills"


@click.group(name="skill")
def skill_group() -> None:
    """Install the review skills for an interactive Claude Code session."""


@skill_group.command(name="install")
@click.option(
    "--dir",
    "destination",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_DIR,
    show_default=str(DEFAULT_DIR),
    help="skills directory to install into",
)
@click.option("--force", is_flag=True, help="overwrite an existing install")
def install(destination: Path, force: bool) -> None:
    """Copy the review and description skills into a skills directory."""
    for name in skills.SKILLS:
        try:
            target = skills.install(destination, force=force, skill=name)
        except FileExistsError as exists:
            fail(f"{exists} already exists; pass --force to replace it")
        click.echo(f"Installed {name} to {target}")
    click.echo(
        "Start a Claude Code session there and ask it to review or describe a change."
    )
