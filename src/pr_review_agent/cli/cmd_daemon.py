"""``pr-review-agent daemon start`` -- run the poll-classify-review loop.

The only verb in the whole command tree that constructs a review engine, and
therefore the only one that can spend allowance. Everything it starts runs
behind the budget governor; this module adds no path around it.

The loop itself lives in :mod:`pr_review_agent.daemon`; this is the argument
parsing, the log configuration and the exit status, nothing else.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from .. import daemon, logs
from .._startup import StartupError
from ._common import config_option, fail, startup_or_exit


@click.group(name="daemon")
def daemon_group() -> None:
    """Run the review daemon."""


@daemon_group.command(name="start")
@config_option
@click.option(
    "--log-level",
    type=click.Choice(logs.LEVELS, case_sensitive=False),
    default=None,
    help=(
        f"how much to log. Overrides {logs.LEVEL_ENV_VAR} and "
        "logging.level in the config file."
    ),
)
@click.option(
    "--log-format",
    type=click.Choice(logs.FORMATS, case_sensitive=False),
    default=None,
    help=(
        f"what a record looks like. Overrides {logs.FORMAT_ENV_VAR} and "
        "logging.format in the config file. auto is text on a terminal and "
        "JSON anywhere else."
    ),
)
def start(config_path: str, log_level: str | None, log_format: str | None) -> None:
    """Poll, classify, review and publish until stopped."""
    # The config is loaded before logging is configured, because it carries
    # the lowest-precedence layer of both settings. Nothing between here and
    # `logs.configure` logs: a startup failure is reported on stderr by
    # `fail`, which does not go through logging at all.
    config, token = startup_or_exit(config_path)
    try:
        level = logs.resolve_level(log_level, config.logging.level)
        fmt = logs.resolve_format(log_format, config.logging.format)
    except (logs.LevelError, logs.FormatError) as exc:
        fail(str(exc))
    logs.configure(level, fmt)
    try:
        asyncio.run(daemon.run(config, token, Path(config_path)))
    except StartupError as exc:
        # The shared budget policy is only readable once the store is open,
        # so this half of "the host is not set up" surfaces from inside the
        # run rather than from `startup_or_exit`. Exit 3 all the same: a
        # complier waiting for its authority is restarted by the unit.
        fail(str(exc))
