"""``pr-review-agent host check`` -- can this machine reach what the daemon needs?

A noun with one verb, because what it asks about is the host and not the
daemon: egress to ``api.github.com`` for polling, to ``github.com`` for the
checkout, to ``api.anthropic.com`` for the review, and a git new enough for
``GIT_CONFIG_GLOBAL`` to be honoured. ``daemon check`` would name the wrong
thing as broken.

The checks themselves live in :mod:`pr_review_agent.bootstrap`; this is the
argument parsing and the exit status, nothing else. No token is ever
printed, and no review engine is constructed: the Anthropic probe is
unauthenticated and any HTTP status counts as a route.
"""

from __future__ import annotations

import asyncio

import click

from .. import bootstrap
from ._common import config_option, startup_or_exit


@click.group(name="host")
def host_group() -> None:
    """Check the deployment host before the daemon runs on it."""


@host_group.command(name="check")
@config_option
def check(config_path: str) -> None:
    """Run the pre-flight checks; exit 1 if any of them fails."""
    config, token = startup_or_exit(config_path)
    status = bootstrap.report(asyncio.run(bootstrap.run_checks(config, token)))
    if status:
        raise SystemExit(status)
