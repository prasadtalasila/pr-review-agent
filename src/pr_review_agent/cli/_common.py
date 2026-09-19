"""The parts every noun module needs, so three copies cannot drift apart.

``--config`` has to mean the same thing and default to the same path in
every command that takes one, and a startup failure has to leave the same
exit status whichever verb hit it. Both are small enough that three
hand-written copies would work and large enough that three hand-written
copies would eventually disagree.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn, TypeVar

import click

from .._startup import StartupError, startup
from ..config import Config

#: Unusable config, missing token, or a refusal to overwrite a file. Not 2:
#: Click spends that on usage errors, and merging the two would make a
#: mistyped command look like a missing credential.
EXIT_STARTUP = 3

#: Where the daemon looks when nothing says otherwise -- and, because
#: ``store.path`` is relative by default too, where ``state.db`` is written.
DEFAULT_CONFIG = "config.yaml"

F = TypeVar("F", bound=Callable)


def config_option(command: F) -> F:
    """Attach the shared ``--config`` option to ``command``."""
    return click.option(
        "--config",
        "config_path",
        default=DEFAULT_CONFIG,
        show_default=True,
        help="path to config.yaml",
    )(command)


def fail(message: str) -> NoReturn:
    """Report an unusable setup on stderr and exit 3."""
    click.echo(message, err=True)
    raise SystemExit(EXIT_STARTUP)


def startup_or_exit(config_path: str) -> tuple[Config, str]:
    """The validated config and the token, or exit 3 naming what is missing.

    Both verbs that talk to GitHub need both halves and report a missing one
    identically. ``config validate`` deliberately does not use this: it needs
    the config and not the token.
    """
    try:
        return startup(config_path)
    except StartupError as exc:
        fail(str(exc))
