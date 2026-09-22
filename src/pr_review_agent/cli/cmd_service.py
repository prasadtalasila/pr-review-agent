"""``pr-review-agent service install`` -- place the systemd user unit.

A *user* unit, because the review engine spawns ``claude``, which finds its
login under ``$HOME``. Running as the human who already ran ``claude login``
means there is no second credential to provision and no ``ProtectHome=`` to
work around. Nothing the daemon does needs root.

This verb is a file writer, not a service manager: it writes the unit and the
directories it names, then prints the ``systemctl`` commands rather than
running them. Shelling out to ``systemctl`` would make the verb untestable on
the two CI platforms that have no systemd, and it would take the decision to
*start* something away from the operator.

It does not write ``config.yaml`` either. ``config generate`` is the only
writer of that file and should stay so; what this prints instead is the exact
invocation, with the two paths that have to change for a service install.
"""

from __future__ import annotations

import os
import stat
import sys
from importlib.resources import files
from pathlib import Path

import click

from ._common import fail

#: The unit, shipped inside the package so an install has it -- the same
#: reason the config templates are there. Addressed through the package
#: rather than as ``pr_review_agent.templates``: that directory holds no
#: ``__init__.py``.
UNIT_TEMPLATE = "pr-review-agent.service"

#: What the unit is called once installed. ``systemctl --user`` and the
#: ``SyslogIdentifier=`` inside the file both spell it this way.
UNIT_NAME = "pr-review-agent.service"


def unit_text() -> str:
    """The unit template, read from the installed package."""
    return (files("pr_review_agent") / "templates" / UNIT_TEMPLATE).read_text(
        encoding="utf-8"
    )


def _xdg(variable: str, default: str) -> Path:
    """An XDG base directory, honouring the environment variable if set.

    systemd's own ``%h``-relative defaults moved between versions -- state
    directories were under ``$XDG_DATA_HOME`` before systemd 256 and under
    ``$XDG_STATE_HOME`` after. So the paths are resolved here and written
    into the unit and the config absolutely, rather than left to a
    ``StateDirectory=`` whose meaning depends on the host's systemd.
    """
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / default


def paths() -> dict[str, Path]:
    """Every path the install touches, resolved once."""
    config_home = _xdg("XDG_CONFIG_HOME", ".config")
    return {
        "config_dir": config_home / "pr-review-agent",
        "config": config_home / "pr-review-agent" / "config.yaml",
        "token_env": config_home / "pr-review-agent" / "token.env",
        "state_dir": _xdg("XDG_STATE_HOME", ".local/state") / "pr-review-agent",
        "cache_dir": _xdg("XDG_CACHE_HOME", ".cache") / "pr-review-agent" / "repos",
        "unit_dir": config_home / "systemd" / "user",
        "unit": config_home / "systemd" / "user" / UNIT_NAME,
    }


def executable() -> Path:
    """The absolute ``pr-review-agent`` systemd should start.

    ``ExecStart=`` must be absolute, and the console script sits beside the
    interpreter running this command in a venv and in a pipx install alike.
    Derived from ``sys.executable`` rather than looked up on ``PATH``,
    because the user manager's ``PATH`` is not the shell's.
    """
    suffix = ".exe" if sys.platform == "win32" else ""
    return Path(sys.executable).parent / f"pr-review-agent{suffix}"


@click.group(name="service")
def service_group() -> None:
    """Install the agent as a systemd user service."""


@service_group.command(name="install")
@click.option("--force", is_flag=True, help="overwrite an existing unit file")
def install(force: bool) -> None:
    """Write the systemd user unit, and the directories it names."""
    where = paths()
    if where["unit"].exists() and not force:
        fail(f"{where['unit']} already exists; pass --force to overwrite it")

    try:
        for key in ("config_dir", "state_dir", "cache_dir", "unit_dir"):
            where[key].mkdir(parents=True, exist_ok=True)
        _write_token_env(where["token_env"])
        where["unit"].write_text(
            unit_text().format(
                executable=executable(),
                config=where["config"],
                token_env=where["token_env"],
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        fail(f"cannot install the unit: {exc}")

    _report(where)


def _write_token_env(path: Path) -> None:
    """Create a 0600 placeholder for ``GITHUB_TOKEN``, once.

    Never overwritten, not even under ``--force``: by the second install it
    holds a real credential, and replacing that with a placeholder would
    break a working deployment in a way no other file here can.
    """
    if path.exists():
        return
    path.write_text("GITHUB_TOKEN=\n", encoding="utf-8")
    # Written before the token is, so the secret is never briefly readable.
    # A no-op on Windows, which has no POSIX mode bits to set.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _report(where: dict[str, Path]) -> None:
    """Print what was written and what the operator still has to do."""
    click.echo(f"wrote {where['unit']}")
    click.echo(f"created {where['state_dir']}")
    click.echo(f"created {where['cache_dir']}")
    click.echo("")
    click.echo("next:")
    click.echo(f"  1. pr-review-agent config generate --output {where['config']}")
    click.echo("     then edit it, setting:")
    click.echo(f"       store.path:            {where['state_dir'] / 'state.db'}")
    click.echo(f"       workspace.cache_dir:   {where['cache_dir']}")
    click.echo(f"  2. put your token in {where['token_env']}  (GITHUB_TOKEN=ghp_...)")
    click.echo(f"  3. pr-review-agent config validate --config {where['config']}")
    click.echo("  4. systemctl --user daemon-reload")
    click.echo("     systemctl --user enable --now pr-review-agent")
    # A user manager stops at logout and does not start at boot without
    # this, which is the one way a user unit is worse than a system one.
    # `$USER` stays literal: this is a line to paste into a shell.
    click.echo("  5. loginctl enable-linger $USER")
    click.echo("")
    click.echo("  journalctl --user -u pr-review-agent -f")
