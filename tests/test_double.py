"""The HTTPS double is load-bearing, so it gets its own test.

If the fixture silently served nothing, every checkout test would still
pass against an empty repository. This is the test that would not.
"""

import subprocess
import sys

import pytest

from conftest import PR_NUMBER

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)


def test_the_double_serves_the_fork_shaped_ref(git_remote, monkeypatch):
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    listed = subprocess.run(
        ["git", "ls-remote", git_remote.url, f"refs/pull/{PR_NUMBER}/head"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert git_remote.head_sha in listed
    assert git_remote.requests, "the double recorded no requests"


def test_the_pull_head_is_on_no_branch(git_remote, monkeypatch):
    """A fork's head is reachable only from refs/pull/N/head."""
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    heads = subprocess.run(
        ["git", "ls-remote", "--heads", git_remote.url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert git_remote.head_sha not in heads
