"""``pr-review-agent config`` -- write a config file, and check one.

This noun is the only writer of ``config.yaml``, and ``generate`` is the
command whose absence made the documented quickstart impossible to follow:
it told the operator to copy a template out of a repository they had not
cloned, from a wheel that had never contained one.

Neither verb reaches the network, and neither constructs a review engine.
``validate`` in particular does not ask for ``GITHUB_TOKEN``: it answers a
question about a file, and demanding a credential to parse YAML would make
it useless on the machine where the file is being written.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import click

from .._startup import StartupError, load_config
from ._common import config_option, fail

#: The two templates, shipped inside the package so an install has them.
#: The minimal one is the default because it is the one an operator edits;
#: the comprehensive one is 269 lines of commented reasoning and is better
#: read than copied.
MINIMAL_TEMPLATE = "config.minimal.example.yaml"
FULL_TEMPLATE = "config.example.yaml"


def template_text(*, full: bool) -> str:
    """The text of the requested template, read from the installed package."""
    name = FULL_TEMPLATE if full else MINIMAL_TEMPLATE
    # Addressed through the package rather than as ``pr_review_agent
    # .templates``: the directory holds no ``__init__.py``, so naming it as a
    # package would rest on namespace-package resolution inside a wheel.
    return (files("pr_review_agent") / "templates" / name).read_text(encoding="utf-8")


@click.group(name="config")
def config_group() -> None:
    """Write and check the agent's configuration file."""


@config_group.command(name="generate")
@click.option(
    "--output",
    "output_path",
    default="config.yaml",
    show_default=True,
    type=click.Path(path_type=Path),
    help="where to write the template",
)
@click.option(
    "--full",
    is_flag=True,
    help="write the commented template showing every key and its default",
)
@click.option("--force", is_flag=True, help="overwrite an existing file")
def generate(output_path: Path, full: bool, force: bool) -> None:
    """Write a config template, to ./config.yaml unless told otherwise."""
    # A config.yaml names real accounts, is gitignored, and will sit beside
    # the agent's credentials. There is no copy of it anywhere, so an
    # accidental second `config generate` must not be how an operator finds
    # that out.
    if output_path.exists() and not force:
        fail(f"{output_path} already exists; pass --force to overwrite it")
    try:
        output_path.write_text(template_text(full=full), encoding="utf-8")
    except OSError as exc:
        # The documented destination is /etc/pr-review-agent/config.yaml, so
        # a missing directory or a root-owned one is the ordinary mistake
        # here, not an impossible one. A traceback would bury which it was.
        fail(f"cannot write {output_path}: {exc}")
    click.echo(f"wrote {output_path}")
    click.echo("edit it, then run: pr-review-agent config validate")


@config_group.command(name="validate")
@config_option
def validate(config_path: str) -> None:
    """Load a config file and report what the loader made of it."""
    try:
        config = load_config(config_path)
    except StartupError as exc:
        fail(str(exc))
    click.echo(f"{config_path} is valid")
    click.echo(f"  repository:  {config.github.repo}")
    click.echo(f"  allowlisted: {len(config.triggers.allowlist.user_ids)} user id(s)")
    click.echo(f"  budget:      enabled={config.budget.enabled}")
