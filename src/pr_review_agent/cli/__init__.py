"""The root ``pr-review-agent`` group, and the exit codes every verb shares.

Every command follows one grammar -- ``pr-review-agent <noun> <verb>`` --
with four nouns: ``config``, ``host``, ``daemon``, ``service``. Each noun
is a Click group in its own ``cmd_<noun>.py`` module, attached here in the
order an operator meets them: write a config, check the host can reach what
the daemon needs, then start it -- and, for an unattended install, hand it to
systemd instead.

``host`` carries a single verb, which the grammar would normally argue
against. It stays a noun of its own because the checks are about the
*machine* -- egress to ``api.github.com``, to ``github.com``, to
``api.anthropic.com``, and a new enough git -- so ``daemon check`` would
misdescribe what has failed when they fail.

**Exit codes.** Click owns 2 for usage errors, and this project used to
spend 2 on "the token or the config file is unusable". Leaving them merged
would make a mistyped command indistinguishable from a missing credential,
so the startup failure moved to 3:

============ ==============================================================
``0``        success
``1``        a ``host check`` check failed
``2``        usage error, including a bare ``pr-review-agent``
``3``        unusable config, missing token, or a refusal to overwrite
============ ==============================================================
"""

from __future__ import annotations

import click

from .cmd_config import config_group
from .cmd_daemon import daemon_group
from .cmd_host import host_group
from .cmd_service import service_group

#: What the operator does, in the order they do it. ``--help`` lists the
#: nouns this way rather than alphabetically, so the help text doubles as
#: the setup sequence.
_WORKFLOW_ORDER = ("config", "host", "daemon", "service")


class WorkflowGroup(click.Group):
    """A group that lists its nouns in workflow order rather than A-Z."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = super().list_commands(ctx)
        ordered = [name for name in _WORKFLOW_ORDER if name in names]
        return ordered + [name for name in names if name not in _WORKFLOW_ORDER]


@click.group(cls=WorkflowGroup, invoke_without_command=True)
@click.version_option(package_name="pr-review-agent")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Review pull requests with an LLM, under an enforced usage budget.

    Commands follow a 'pr-review-agent <noun> <verb>' grammar, grouped by
    the setup workflow: config -> host -> daemon, plus 'service' to run it
    under systemd instead of by hand.

    \b
    First-time setup:
      1.  pr-review-agent config generate      # write config.yaml
      2.  # edit config.yaml (repo, allowlist, budget)
      3.  pr-review-agent config validate      # check for errors
      4.  GITHUB_TOKEN=... pr-review-agent host check
      5.  GITHUB_TOKEN=... pr-review-agent daemon start

    To run it unattended, 'pr-review-agent service install' writes a
    systemd user unit instead of step 5.
    """
    if ctx.invoked_subcommand is not None:
        return
    # Not `echo_help(); exit 0`. Before 0.14 a bare `pr-review-agent` started
    # the daemon, so the spelling is live in systemd units; a zero exit would
    # turn one of those into a restart loop that reports success on every
    # pass. Failing here is the one way the unit's owner finds out.
    raise click.UsageError("a command is required; did you mean 'daemon start'?")


cli.add_command(config_group)
cli.add_command(host_group)
cli.add_command(daemon_group)
cli.add_command(service_group)

#: The ``pr-review-agent`` console script. A Click group is already callable
#: as one, so there is nothing for a wrapper to add.
main = cli
