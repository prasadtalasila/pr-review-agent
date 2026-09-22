"""The command tree: its shape, its exit codes, and the file it writes.

The most load-bearing test here is
``test_the_generated_config_loads``. Before 0.14 the quickstart told an
operator to copy a template the distribution did not contain, and no unit
test could see it: every test imports from the source tree, where both
templates have always been on disk. That one closes the loop the reported
bug went through -- generate a file the way an operator does, then feed it
to the loader.
"""

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from pr_review_agent import bootstrap
from pr_review_agent._startup import TOKEN_ENV
from pr_review_agent.bootstrap import CheckResult
from pr_review_agent.cli import cli
from pr_review_agent.cli._common import EXIT_STARTUP
from pr_review_agent.cli.cmd_config import (
    FULL_TEMPLATE,
    MINIMAL_TEMPLATE,
    template_text,
)
from pr_review_agent.config import Config

ROOT = Path(__file__).parent.parent
PACKAGED = ROOT / "src" / "pr_review_agent" / "templates"

CONFIG_YAML = """
github:
  repo: prasadtalasila/pr-review-agent
  agent_user_id: 42
triggers:
  allowlist: [114395272]
budget:
  session_tokens: 88000
  weekly_tokens: 1500000
  max_run_tokens: 60000
engine:
  model: claude-sonnet-5
  expected_version: '2.1.274'
  timeout_seconds: 900
"""


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Invoke the CLI with ``tmp_path`` as the working directory."""
    monkeypatch.chdir(tmp_path)

    def invoke(*args):
        return CliRunner().invoke(cli, list(args))

    return invoke


def write_config(tmp_path, text=CONFIG_YAML):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _canned(results):
    async def run_checks(_config, _token):
        return results

    return run_checks


# -- the grammar ---------------------------------------------------------


def test_the_command_tree_is_exactly_four_nouns_and_five_verbs():
    """A new verb is a deliberate act, not something a refactor adds.

    The spend rule is that nothing reaches a review engine outside the
    budget governor. Pinning the tree means a command that could is a
    failing test rather than a review comment nobody wrote.
    """
    ctx = cli.make_context("pr-review-agent", [], resilient_parsing=True)
    tree = {
        name: sorted(cli.commands[name].commands)  # type: ignore[attr-defined]
        for name in cli.list_commands(ctx)
    }
    assert tree == {
        "config": ["generate", "validate"],
        "host": ["check"],
        "daemon": ["start"],
        "service": ["install"],
    }


def test_the_nouns_are_listed_in_workflow_order():
    """``--help`` should read as the setup sequence, not alphabetically.

    Asserted on ``list_commands`` rather than on where each noun first
    appears in the help text: the docstring names them in the same order,
    so a text search would pass with the ordering removed.
    """
    ctx = cli.make_context("pr-review-agent", [], resilient_parsing=True)
    assert cli.list_commands(ctx) == ["config", "host", "daemon", "service"]


def test_a_bare_invocation_fails_rather_than_printing_help(run):
    """Exit 0 would turn an unmigrated systemd unit into a restart loop
    that reports success on every pass."""
    result = run()
    assert result.exit_code == 2
    assert "daemon start" in result.output


# -- config generate -----------------------------------------------------


def test_generate_writes_the_minimal_template_by_default(run, tmp_path):
    result = run("config", "generate")
    assert result.exit_code == 0
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert written == (PACKAGED / MINIMAL_TEMPLATE).read_text(encoding="utf-8")


def test_generate_writes_the_comprehensive_template_with_full(run, tmp_path):
    assert run("config", "generate", "--full").exit_code == 0
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert written == (PACKAGED / FULL_TEMPLATE).read_text(encoding="utf-8")


def test_generate_honours_output(run, tmp_path):
    target = tmp_path / "etc" / "agent.yaml"
    target.parent.mkdir()
    assert run("config", "generate", "--output", str(target)).exit_code == 0
    assert target.exists()
    assert not (tmp_path / "config.yaml").exists()


def test_generate_refuses_to_overwrite(run, tmp_path):
    """A config.yaml names real accounts, is gitignored, and has no copy."""
    existing = write_config(tmp_path, "github:\n  repo: mine/own\n")
    result = run("config", "generate")
    assert result.exit_code == EXIT_STARTUP
    assert "already exists" in result.output
    assert existing.read_text(encoding="utf-8") == "github:\n  repo: mine/own\n"


def test_force_overwrites(run, tmp_path):
    write_config(tmp_path, "github:\n  repo: mine/own\n")
    assert run("config", "generate", "--force").exit_code == 0
    assert "agent_user_id" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_generate_reports_an_unwritable_destination(run, tmp_path):
    """`--output /etc/pr-review-agent/config.yaml` before the directory
    exists is the ordinary mistake, and must not be a traceback."""
    target = tmp_path / "nonexistent" / "config.yaml"
    result = run("config", "generate", "--output", str(target))
    assert result.exit_code == EXIT_STARTUP
    assert "cannot write" in result.output


def test_the_generated_config_loads(run, tmp_path):
    """The regression test for the bug this release exists to fix."""
    assert run("config", "generate").exit_code == 0
    config = Config.load(tmp_path / "config.yaml")
    assert config.github.repo == "prasadtalasila/pr-review-agent"


def test_the_generated_full_config_loads(run, tmp_path):
    assert run("config", "generate", "--full").exit_code == 0
    assert Config.load(tmp_path / "config.yaml").github.repo


# -- config validate -----------------------------------------------------


def test_validate_needs_no_token(run, tmp_path, monkeypatch):
    """It answers a question about a file. Demanding a credential to parse
    YAML would make it useless on the machine where the file is written."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    write_config(tmp_path)
    result = run("config", "validate")
    assert result.exit_code == 0
    assert "is valid" in result.output
    assert "prasadtalasila/pr-review-agent" in result.output


def test_validate_reports_a_broken_file(run, tmp_path):
    write_config(tmp_path, "github:\n  repo: not-a-repo\n")
    result = run("config", "validate")
    assert result.exit_code == EXIT_STARTUP


def test_validate_reports_a_missing_file(run):
    result = run("config", "validate", "--config", "absent.yaml")
    assert result.exit_code == EXIT_STARTUP
    assert "cannot read config" in result.output


# -- host check ----------------------------------------------------------


def test_host_check_passes(run, tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    monkeypatch.setattr(
        bootstrap,
        "run_checks",
        _canned([CheckResult("github open_pulls", True, "4987/5000 remaining")]),
    )
    write_config(tmp_path)
    result = run("host", "check")
    assert result.exit_code == 0
    assert "PASS  github open_pulls" in result.output


def test_host_check_exits_one_when_a_check_fails(run, tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    monkeypatch.setattr(
        bootstrap,
        "run_checks",
        _canned([CheckResult("anthropic reachable", False, "no route: blocked")]),
    )
    write_config(tmp_path)
    assert run("host", "check").exit_code == 1


def test_host_check_without_a_token_exits_three(run, tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    write_config(tmp_path)
    result = run("host", "check")
    assert result.exit_code == EXIT_STARTUP
    assert TOKEN_ENV in result.output


def test_host_check_with_an_unreadable_config_exits_three(run, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    result = run("host", "check", "--config", "absent.yaml")
    assert result.exit_code == EXIT_STARTUP
    assert "cannot read config" in result.output


# -- daemon start --------------------------------------------------------


def test_daemon_start_without_a_token_exits_three(run, tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    write_config(tmp_path)
    result = run("daemon", "start")
    assert result.exit_code == EXIT_STARTUP
    assert TOKEN_ENV in result.output


def test_daemon_start_with_an_unreadable_config_exits_three(run, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    result = run("daemon", "start", "--config", "missing.yaml")
    assert result.exit_code == EXIT_STARTUP
    assert "cannot read config" in result.output


def test_only_daemon_start_can_reach_a_review_engine(run, tmp_path, monkeypatch):
    """Nothing may call a review engine outside the budget governor, so no
    verb but the one that runs the daemon may even construct one."""
    built = []
    monkeypatch.setattr(
        "pr_review_agent.daemon.build_engine", lambda config: built.append(config)
    )
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    monkeypatch.setattr(bootstrap, "run_checks", _canned([]))
    write_config(tmp_path)

    run("config", "generate", "--force")
    run("config", "validate")
    run("host", "check")

    assert built == []


def _capture_level(monkeypatch, run, tmp_path, *args):
    """Start the daemon far enough to settle level and format, then stop.

    Both are captured, because both are resolved in the same place and a
    change to either has to say what it did to the other.
    """
    seen = []
    monkeypatch.setenv(TOKEN_ENV, "fake-token")
    monkeypatch.setattr(
        "pr_review_agent.logs.configure",
        lambda level, fmt: seen.append((level, fmt)),
    )
    monkeypatch.setattr(
        "pr_review_agent.cli.cmd_daemon.asyncio.run", lambda coro: coro.close()
    )
    write_config(tmp_path)
    result = run("daemon", "start", *args)
    return seen, result


def test_the_log_level_flag_beats_the_environment(run, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEW_AGENT_LOG_LEVEL", "WARNING")
    seen, _ = _capture_level(monkeypatch, run, tmp_path, "--log-level", "DEBUG")
    assert seen == [("DEBUG", "auto")]


def test_the_environment_sets_the_level_with_no_flag(run, tmp_path, monkeypatch):
    """What a systemd unit uses: the level lands in the `Environment=` block
    that already carries GITHUB_TOKEN, without touching `ExecStart=`."""
    monkeypatch.setenv("PR_REVIEW_AGENT_LOG_LEVEL", "WARNING")
    seen, _ = _capture_level(monkeypatch, run, tmp_path)
    assert seen == [("WARNING", "auto")]


def test_the_default_level_is_info(run, tmp_path, monkeypatch):
    monkeypatch.delenv("PR_REVIEW_AGENT_LOG_LEVEL", raising=False)
    seen, _ = _capture_level(monkeypatch, run, tmp_path)
    assert seen == [("INFO", "auto")]


def test_a_typo_in_the_environment_level_exits_three(run, tmp_path, monkeypatch):
    """Not a silent fall-back to INFO: an operator who asked for DEBUG during
    an incident has to learn that they did not get it."""
    monkeypatch.setenv("PR_REVIEW_AGENT_LOG_LEVEL", "VERBOSE")
    seen, result = _capture_level(monkeypatch, run, tmp_path)
    assert seen == []
    assert result.exit_code == EXIT_STARTUP
    assert "PR_REVIEW_AGENT_LOG_LEVEL" in result.output


def test_a_bad_log_level_flag_is_a_usage_error(run, tmp_path, monkeypatch):
    seen, result = _capture_level(monkeypatch, run, tmp_path, "--log-level", "VERBOSE")
    assert seen == []
    assert result.exit_code == 2


def test_the_log_format_flag_beats_the_environment(run, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEW_AGENT_LOG_FORMAT", "text")
    seen, _ = _capture_level(monkeypatch, run, tmp_path, "--log-format", "json")
    assert seen == [("INFO", "json")]


def test_the_environment_sets_the_format_with_no_flag(run, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEW_AGENT_LOG_FORMAT", "text")
    seen, _ = _capture_level(monkeypatch, run, tmp_path)
    assert seen == [("INFO", "text")]


def test_a_typo_in_the_environment_format_exits_three(run, tmp_path, monkeypatch):
    """Same refusal as the level: a shape that quietly fell back to the
    default is a stream something downstream cannot parse."""
    monkeypatch.setenv("PR_REVIEW_AGENT_LOG_FORMAT", "jsonl")
    seen, result = _capture_level(monkeypatch, run, tmp_path)
    assert seen == []
    assert result.exit_code == EXIT_STARTUP
    assert "PR_REVIEW_AGENT_LOG_FORMAT" in result.output


def test_a_bad_log_format_flag_is_a_usage_error(run, tmp_path, monkeypatch):
    seen, result = _capture_level(monkeypatch, run, tmp_path, "--log-format", "jsonl")
    assert seen == []
    assert result.exit_code == 2


# -- the two copies of each template -------------------------------------


@pytest.mark.parametrize("name", [MINIMAL_TEMPLATE, FULL_TEMPLATE])
def test_the_packaged_template_matches_the_one_at_the_repository_root(name):
    """Two copies exist on purpose: the root one keeps the documentation's
    links and the clone workflow working, the packaged one is what an
    install has. This is the guard that stops them drifting."""
    assert (PACKAGED / name).read_bytes() == (ROOT / name).read_bytes()


@pytest.mark.parametrize("full", [False, True])
def test_both_templates_are_readable_through_importlib_resources(full):
    """Read the way an installed copy is, not off the source tree."""
    assert yaml.safe_load(template_text(full=full))["github"]["repo"]
