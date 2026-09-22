"""The systemd user unit, and the verb that installs it.

Two of these exist because of how systemd fails rather than how it works.
``test_the_unit_sets_neither_standard_stream`` pins the one setting whose
*presence* breaks journald detection (systemd/systemd#6800), and
``test_the_unit_does_not_protect_home`` pins a hardening directive that
would hide the state database, the checkout cache and the `claude` login in
one go. Both are silent failures: the unit starts either way.

The rest is ordinary: the template has to ship in the distribution, the
paths have to be absolute, and a second install must not overwrite the file
holding the token.
"""

import configparser
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from pr_review_agent import logs
from pr_review_agent.cli import cli
from pr_review_agent.cli._common import EXIT_STARTUP
from pr_review_agent.cli.cmd_service import UNIT_TEMPLATE, executable, unit_text

ROOT = Path(__file__).resolve().parent.parent
PACKAGED = ROOT / "src" / "pr_review_agent" / "templates" / UNIT_TEMPLATE


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway ``$HOME`` with no XDG overrides pointing out of it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


def install(*args):
    return CliRunner().invoke(cli, ["service", "install", *args])


def unit_at(home_dir):
    return home_dir / ".config" / "systemd" / "user" / "pr-review-agent.service"


def parsed(text):
    """The unit as an INI document, with systemd's duplicate keys allowed."""
    # RawConfigParser, not ConfigParser: `%h` and `$MAINPID` are systemd
    # specifiers and must not be read as interpolation. strict=False because
    # systemd allows a directive to repeat.
    #
    # Key lookups below are case-insensitive, which configparser applies to
    # both sides -- so `"StandardOutput" not in section` means what it reads
    # as, and is not a spelling that can pass by accident.
    parser = configparser.RawConfigParser(strict=False)
    parser.read_string(text)
    return parser


# --- the template ships ------------------------------------------------


def test_the_unit_template_is_on_disk():
    assert PACKAGED.is_file()


def test_the_unit_template_is_listed_for_the_wheel_and_the_sdist():
    """The config templates shipped in no wheel for thirteen releases."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for fmt in ("sdist", "wheel"):
        assert f'templates/*.service", format = "{fmt}"' in pyproject


def test_unit_text_reads_the_packaged_file():
    assert unit_text() == PACKAGED.read_text(encoding="utf-8")


# --- what the unit must and must not say -------------------------------


def test_the_unit_sets_neither_standard_stream():
    """Setting StandardError= opens a second stream and breaks detection.

    The defaults -- StandardOutput=journal, StandardError=inherit -- make
    both descriptors the same socket, which is the only arrangement
    JOURNAL_STREAM can name unambiguously (systemd/systemd#6800).
    """
    service = parsed(unit_text())["Service"]
    assert "StandardOutput" not in service
    assert "StandardError" not in service


def test_the_unit_sets_the_log_level_by_environment_not_by_execstart():
    """The line `systemctl --user edit` is meant to change.

    Verbosity must not mean editing ExecStart=, and a `--log-level` flag
    there would silently beat the override an operator just wrote.
    """
    service = parsed(unit_text())["Service"]
    assert service["Environment"] == f"{logs.LEVEL_ENV_VAR}={logs.DEFAULT_LEVEL}"
    assert "--log-level" not in service["ExecStart"]


def test_the_units_log_level_is_one_the_daemon_accepts():
    """A level the loader would refuse leaves a unit that cannot start."""
    _, _, level = parsed(unit_text())["Service"]["Environment"].partition("=")
    assert logs.parse_level(level, source="the unit") == level


def test_the_unit_does_not_protect_home():
    """ProtectHome= would hide the state db, the cache and the claude login."""
    assert "ProtectHome" not in parsed(unit_text())["Service"]


def test_the_unit_is_a_user_unit():
    """default.target, not multi-user.target: a user manager has no such target."""
    assert parsed(unit_text())["Install"]["WantedBy"] == "default.target"


def test_the_unit_reloads_on_sighup():
    """daemon.py reloads budget and publish on SIGHUP; a restart would not."""
    assert "-HUP $MAINPID" in parsed(unit_text())["Service"]["ExecReload"]


def test_the_unit_names_itself_for_the_journal():
    """Without this the identifier is derived from a venv path."""
    service = parsed(unit_text())["Service"]
    assert service["SyslogIdentifier"] == "pr-review-agent"
    assert service["Type"] == "exec"
    assert service["NoNewPrivileges"] == "yes"


def test_the_unit_carries_no_logs_directory():
    """The daemon never opens a log file; journald owns the destination."""
    assert "LogsDirectory" not in parsed(unit_text())["Service"]


# --- installing it -----------------------------------------------------


def test_install_writes_the_unit_and_the_directories(home):
    result = install()
    assert result.exit_code == 0, result.output
    assert unit_at(home).is_file()
    assert (home / ".local" / "state" / "pr-review-agent").is_dir()
    assert (home / ".cache" / "pr-review-agent" / "repos").is_dir()


def test_the_installed_unit_carries_absolute_paths(home):
    assert install().exit_code == 0
    service = parsed(unit_at(home).read_text(encoding="utf-8"))["Service"]
    command, _, _ = service["ExecStart"].partition(" daemon start")
    # systemd rejects a relative ExecStart= outright, and resolves neither
    # EnvironmentFile= nor --config against any directory but /.
    assert Path(command).is_absolute()
    assert command == str(executable())
    assert Path(service["EnvironmentFile"]).is_absolute()
    assert str(home) in service["EnvironmentFile"]


def test_the_installed_unit_leaves_no_placeholder_behind(home):
    assert install().exit_code == 0
    assert "{" not in unit_at(home).read_text(encoding="utf-8")


def test_install_honours_the_xdg_variables(tmp_path, home, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "elsewhere"))
    assert install().exit_code == 0
    assert (tmp_path / "elsewhere" / "pr-review-agent").is_dir()


def test_a_second_install_is_refused(home):
    assert install().exit_code == 0
    result = install()
    assert result.exit_code == EXIT_STARTUP
    assert "--force" in result.output


def test_force_overwrites_the_unit(home):
    assert install().exit_code == 0
    unit_at(home).write_text("clobbered", encoding="utf-8")
    assert install("--force").exit_code == 0
    assert "clobbered" not in unit_at(home).read_text(encoding="utf-8")


def test_the_token_file_is_never_overwritten(home):
    """By the second install it holds a credential, not a placeholder."""
    assert install().exit_code == 0
    token_env = home / ".config" / "pr-review-agent" / "token.env"
    token_env.write_text("GITHUB_TOKEN=real\n", encoding="utf-8")
    assert install("--force").exit_code == 0
    assert token_env.read_text(encoding="utf-8") == "GITHUB_TOKEN=real\n"


@pytest.mark.skipif(sys.platform == "win32", reason="no POSIX mode bits")
def test_the_token_file_is_not_readable_by_anyone_else(home):
    assert install().exit_code == 0
    token_env = home / ".config" / "pr-review-agent" / "token.env"
    assert stat.S_IMODE(token_env.stat().st_mode) == 0o600


def test_install_prints_the_config_paths_it_did_not_write(home):
    """`config generate` stays the only writer of config.yaml."""
    result = install()
    assert not (home / ".config" / "pr-review-agent" / "config.yaml").exists()
    assert "config generate --output" in result.output
    state_db = home / ".local" / "state" / "pr-review-agent" / "state.db"
    assert str(state_db) in result.output
    assert "loginctl enable-linger" in result.output


# --- the documentation it points at ------------------------------------


def test_service_md_is_in_the_mkdocs_nav():
    nav = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))["nav"]
    assert "SERVICE.md" in yaml.safe_dump(nav)


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed"
)
def test_systemd_accepts_the_installed_unit(home):
    """Runs on a Linux CI runner; skipped on macOS, Windows and containers."""
    assert install().exit_code == 0
    # The executable ExecStart= names does not exist under a test HOME, and
    # `verify` treats that as an error, so point it at something that does.
    text = unit_at(home).read_text(encoding="utf-8")
    unit_at(home).write_text(
        text.replace(str(executable()), sys.executable), encoding="utf-8"
    )
    (home / ".config" / "pr-review-agent" / "token.env").touch()
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(unit_at(home))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
