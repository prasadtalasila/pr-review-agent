"""``pr-review-agent daemon start`` -- run the poll-classify-review loop.

The only verb in the whole command tree that constructs a review engine, and
therefore the only one that can spend allowance. Everything it starts runs
behind the budget governor; this module adds no path around it.

The loop itself lives in :mod:`pr_review_agent.daemon`; this is the argument
parsing, the log configuration and the exit status, nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from .. import daemon
from ._common import config_option, startup_or_exit

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


@click.group(name="daemon")
def daemon_group() -> None:
    """Run the review daemon."""


@daemon_group.command(name="start")
@config_option
def start(config_path: str) -> None:
    """Poll, classify, review and publish until stopped."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    config, token = startup_or_exit(config_path)
    asyncio.run(daemon.run(config, token, Path(config_path)))
