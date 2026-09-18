"""The one place the agent shells out, and the environment it builds."""

import sys

import pytest

from pr_review_agent.workspace.gitcmd import (
    ALLOWED_PROTOCOL,
    MINIMUM_GIT_VERSION,
    GitCommandError,
    git_environment,
    git_version,
    run_git,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)


def test_the_environment_is_built_not_inherited(monkeypatch):
    # GIT_SSH_COMMAND is the shape of the hazard: an inherited variable that
    # names a command git would run.
    monkeypatch.setenv("GIT_SSH_COMMAND", "touch /tmp/pwned")
    monkeypatch.setenv("SOMETHING_ELSE", "1")
    env = git_environment()
    assert "GIT_SSH_COMMAND" not in env
    assert "SOMETHING_ELSE" not in env


def test_the_protocol_whitelist_is_https_and_is_a_constant():
    assert ALLOWED_PROTOCOL == "https"
    assert git_environment()["GIT_ALLOW_PROTOCOL"] == "https"


def test_the_host_gitconfig_is_neutralised_but_home_is_passed(monkeypatch):
    # HOME is passed through so a hostile ~/.gitconfig can be planted in the
    # safety tests; GIT_CONFIG_GLOBAL is what makes planting it harmless.
    monkeypatch.setenv("HOME", "/home/someone")
    env = git_environment()
    assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"]
    assert env["HOME"] == "/home/someone"


def test_the_ca_bundle_is_passed_through(monkeypatch):
    # A TLS-inspecting proxy presents its own certificate, which is exactly
    # the deployment DESIGN.md worries about.
    monkeypatch.setenv("GIT_SSL_CAINFO", "/etc/ssl/corporate.pem")
    assert git_environment()["GIT_SSL_CAINFO"] == "/etc/ssl/corporate.pem"


async def test_run_git_returns_stdout():
    assert "git version" in await run_git("--version")


async def test_a_failing_command_raises_carrying_its_stderr(tmp_path):
    with pytest.raises(GitCommandError) as excinfo:
        await run_git("rev-parse", "HEAD", cwd=tmp_path)
    assert excinfo.value.returncode != 0
    assert "rev-parse" in str(excinfo.value)
    assert excinfo.value.stderr


async def test_a_timeout_raises_rather_than_hanging(tmp_path):
    with pytest.raises(GitCommandError, match="timed out"):
        await run_git("help", "--all", cwd=tmp_path, timeout=0.0)


async def test_a_missing_binary_is_a_git_command_error(tmp_path, monkeypatch):
    monkeypatch.setattr("pr_review_agent.workspace.gitcmd.GIT", "git-does-not-exist")
    with pytest.raises(GitCommandError):
        await run_git("--version", cwd=tmp_path)


async def test_the_git_version_is_readable():
    assert await git_version() >= (2, 0)


def test_the_minimum_version_is_the_one_that_added_config_overrides():
    # GIT_CONFIG_GLOBAL/SYSTEM arrived in 2.32; below it they are ignored
    # silently, taking the whole hardening with them.
    assert MINIMUM_GIT_VERSION == (2, 32)
