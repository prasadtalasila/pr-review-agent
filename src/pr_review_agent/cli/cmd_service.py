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

``--instance`` writes a systemd *template* unit instead, for the deployment
where several repositories share one token budget. One process per repository
is what keeps each one's GitHub token out of every other one's address space,
so the trust boundary is the operating system's rather than this code's.
"""

from __future__ import annotations

import os
import re
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

#: The systemd *template* unit, for several repositories sharing one budget.
#: One file serves every instance; ``%i`` picks the configuration directory.
INSTANCE_UNIT_TEMPLATE = "pr-review-agent@.service"

#: What the unit is called once installed. ``systemctl --user`` and the
#: ``SyslogIdentifier=`` inside the file both spell it this way.
UNIT_NAME = "pr-review-agent.service"
INSTANCE_UNIT_NAME = "pr-review-agent@.service"

#: What an instance name may be. It becomes both a path segment under the
#: configuration directory and half of a systemd unit name, so it is refused
#: rather than escaped: a leading character that must be alphanumeric rules
#: out ``.`` and ``..``, and the set rules out ``/``.
INSTANCE_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def unit_text(template: str = UNIT_TEMPLATE) -> str:
    """A unit template, read from the installed package."""
    return (files("pr_review_agent") / "templates" / template).read_text(
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


def paths(instance: str | None = None) -> dict[str, Path]:
    """Every path the install touches, resolved once.

    With an ``instance``, the configuration and the checkout cache move under
    a directory of that name and the unit becomes the template. Which of the
    two directories is shared between instances is the whole of the
    multi-repository layout, and they go opposite ways:

    ``state_dir`` is **shared on purpose**. One SQLite file is what makes one
    token budget, because the ledger carries no repository column.

    ``cache_dir`` is **never shared**. ``Workspace.sweep`` deletes
    ``<cache_dir>/runs`` at startup to clear what a crash left behind, which
    is safe only while no other checkout of ours is live -- so a second
    daemon pointed at the same cache would delete the first's running
    review mid-flight.
    """
    config_home = _xdg("XDG_CONFIG_HOME", ".config")
    config_root = config_home / "pr-review-agent"
    config_dir = config_root if instance is None else config_root / instance
    cache_root = _xdg("XDG_CACHE_HOME", ".cache") / "pr-review-agent"
    cache_dir = cache_root if instance is None else cache_root / instance
    unit_name = UNIT_NAME if instance is None else INSTANCE_UNIT_NAME
    return {
        "config_root": config_root,
        "config_dir": config_dir,
        "config": config_dir / "config.yaml",
        "token_env": config_dir / "token.env",
        "state_dir": _xdg("XDG_STATE_HOME", ".local/state") / "pr-review-agent",
        "cache_dir": cache_dir / "repos",
        "unit_dir": config_home / "systemd" / "user",
        "unit": config_home / "systemd" / "user" / unit_name,
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
@click.option(
    "--instance",
    default=None,
    help=(
        "install the template unit for one repository of several sharing a "
        "budget, e.g. --instance web"
    ),
)
def install(force: bool, instance: str | None) -> None:
    """Write the systemd user unit, and the directories it names."""
    if instance is not None and not INSTANCE_PATTERN.match(instance):
        fail(
            f"{instance!r} is not a usable instance name: it becomes a "
            "directory and half a unit name, so it must start with a letter "
            "or digit and hold only letters, digits, '_', '.' and '-'"
        )
    where = paths(instance)
    text = _unit_text(instance, where)
    unit = where["unit"]
    if unit.exists() and not force and not _may_rewrite(unit, text, instance):
        fail(f"{unit} already exists; pass --force to overwrite it")

    try:
        for key in ("config_dir", "state_dir", "cache_dir", "unit_dir"):
            where[key].mkdir(parents=True, exist_ok=True)
        _write_token_env(where["token_env"])
        where["unit"].write_text(text, encoding="utf-8")
    except OSError as exc:
        fail(f"cannot install the unit: {exc}")

    _report(where, instance)


def _may_rewrite(unit: Path, text: str, instance: str | None) -> bool:
    """Whether an existing unit may be rewritten without ``--force``.

    Only a template unit, and only with what it already says: one file serves
    every instance, so installing the second repository of a fleet rewrites it
    byte for byte. Anything else is the collision ``--force`` is for, and a
    plain unit stays protected however identical the text.
    """
    return instance is not None and unit.read_text(encoding="utf-8") == text


def _unit_text(instance: str | None, where: dict[str, Path]) -> str:
    """The unit to write, with its absolute paths substituted in.

    The template unit takes the directory *above* the instance rather than
    the config path itself: ``%i`` supplies the rest, and systemd expands it
    per instance long after this command has exited.
    """
    if instance is None:
        return unit_text().format(
            executable=executable(),
            config=where["config"],
            token_env=where["token_env"],
        )
    return unit_text(INSTANCE_UNIT_TEMPLATE).format(
        executable=executable(), config_root=where["config_root"]
    )


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


def _report(where: dict[str, Path], instance: str | None = None) -> None:
    """Print what was written and what the operator still has to do."""
    service = "pr-review-agent" if instance is None else f"pr-review-agent@{instance}"
    click.echo(f"wrote {where['unit']}")
    click.echo(f"created {where['state_dir']}")
    click.echo(f"created {where['cache_dir']}")
    click.echo("")
    click.echo("next:")
    click.echo(f"  1. pr-review-agent config generate --output {where['config']}")
    click.echo("     then edit it, setting:")
    click.echo(f"       store.path:            {where['state_dir'] / 'state.db'}")
    click.echo(f"       workspace.cache_dir:   {where['cache_dir']}")
    if instance is not None:
        _report_instance_rules()
    click.echo(f"  2. put your token in {where['token_env']}  (GITHUB_TOKEN=ghp_...)")
    click.echo(f"  3. pr-review-agent config validate --config {where['config']}")
    click.echo("  4. systemctl --user daemon-reload")
    click.echo(f"     systemctl --user enable --now {service}")
    # A user manager stops at logout and does not start at boot without
    # this, which is the one way a user unit is worse than a system one.
    # `$USER` stays literal: this is a line to paste into a shell.
    click.echo("  5. loginctl enable-linger $USER")
    click.echo("")
    click.echo(f"  journalctl --user -u {service} -f")


def _report_instance_rules() -> None:
    """The three rules a fleet gets wrong, printed where they are acted on.

    Each is a silent failure rather than a loud one, which is why they are
    said here rather than left to the documentation.
    """
    click.echo("")
    click.echo("     sharing a budget across instances:")
    click.echo("       - store.path must be THE SAME file for every instance.")
    click.echo("         One store is one ledger, and one ledger is one budget.")
    click.echo("       - workspace.cache_dir must DIFFER for every instance.")
    click.echo("         A startup sweep clears <cache_dir>/runs, so a shared")
    click.echo("         one would delete another instance's running review.")
    click.echo("       - budget.authority: true on EXACTLY ONE instance, and")
    click.echo("         false on the rest. It publishes the limits they all")
    click.echo("         govern by; a second one refuses to start.")
    click.echo("")
