"""Escapes that are made available, and must not fire.

Every test here plants a real hazard and asserts the checkout is unmoved.
Each one was checked by removing its mitigation and watching it fail: a
safety test that passes either way pins nothing, which is how two earlier
candidates were caught and dropped.

The two that were dropped, so they are not reinvented:

* a hostile ``GIT_CONFIG_GLOBAL`` planted in ``os.environ`` -- it never
  reaches a child whose environment is built from nothing, so deleting the
  mitigation just removed the variable and git read ``$HOME/.gitconfig``
  anyway. Planting it at ``HOME`` is what makes it bite.
* an ``ext::``-shaped submodule gitlink -- ``worktree add`` never populates
  submodules, so that test passed with *every* mitigation removed.
"""

import os
import shutil
import sys

import pytest
from conftest import PR_NUMBER, git

from pr_review_agent.workspace import PullRequestFacts
from pr_review_agent.workspace.gitcmd import GitCommandError, run_git

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)

CAPS = {"max_changed_files": 100, "max_changed_lines": 5000}


def facts(remote) -> PullRequestFacts:
    return PullRequestFacts(
        number=PR_NUMBER,
        head_sha=remote.head_sha,
        base_ref=remote.base_ref,
        additions=2,
        deletions=0,
        changed_files=1,
    )


def add_to_pull_head(remote, mutate) -> None:
    """Put attacker-controlled content at ``refs/pull/N/head``.

    The double serves a bare repository, so the change is made in a scratch
    clone and written back into the served ref -- which is what a fork
    pushing to its own branch looks like from the base repository's side.
    """
    work = remote.serve_root.parent / "mutate"
    shutil.rmtree(work, ignore_errors=True)
    git("clone", "-q", str(remote.serve_root), str(work))
    git("checkout", "-q", remote.head_sha, cwd=work)
    mutate(work)
    git("add", "-A", cwd=work)
    git("commit", "-qm", "hostile", cwd=work)
    remote.head_sha = git("rev-parse", "HEAD", cwd=work)
    git(
        "push",
        "--quiet",
        "--force",
        str(remote.serve_root),
        f"HEAD:refs/pull/{PR_NUMBER}/head",
        cwd=work,
    )


async def test_a_hostile_home_gitconfig_never_fires(
    workspace, git_remote, tmp_path, monkeypatch
):
    """Hooks, a smudge filter, fsmonitor and diff.external, all at once.

    All four are configured in the user's gitconfig, which is why nulling
    that file is the control rather than a flag per mechanism. HOME is
    passed through to the child, so this is planted where git really looks.
    """
    home = tmp_path / "home"
    hooks = tmp_path / "hooks"
    home.mkdir()
    hooks.mkdir()
    marker = tmp_path / "fired"
    fire = tmp_path / "fire.sh"
    fire.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    fire.chmod(0o755)
    for name in ("post-checkout", "reference-transaction", "post-index-change"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    (home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {fire}\n"
        f'[filter "evil"]\n\tsmudge = {fire}\n\tclean = {fire}\n'
        f"[diff]\n\texternal = {fire}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    add_to_pull_head(
        git_remote, lambda repo: (repo / ".gitattributes").write_text("* filter=evil\n")
    )

    async with workspace.checkout(facts(git_remote), **CAPS):
        pass

    assert not marker.exists(), "a hostile gitconfig fired during the checkout"


async def test_a_symlink_checks_out_as_a_regular_file(workspace, git_remote, tmp_path):
    """The exfiltration path: `AGENTS.md -> ~/.claude/.credentials.json`.

    The reviewer reads the tree and can quote what it read into a public
    comment, so a live symlink out of the worktree is a leak that needs no
    execution at all. Nothing downstream can undo it, which is why the
    checkout refuses it rather than trusting a later layer to notice.
    """
    secret = tmp_path / "hostsecret"
    secret.write_text("SUPER SECRET\n")
    add_to_pull_head(git_remote, lambda repo: os.symlink(secret, repo / "AGENTS.md"))

    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        link = checkout.path / "AGENTS.md"
        assert not link.is_symlink()
        contents = link.read_text()
        assert "SUPER SECRET" not in contents
        assert str(secret) in contents


async def test_a_gitattributes_cannot_blank_the_diff(workspace, git_remote):
    """`*.py -diff` renders the PR's own changes as "Binary files differ".

    The reviewer would see nothing while the API's addition count still
    looked normal -- a content-hiding attack on the review that needs no
    execution. Computing the diff in the bare mirror is what stops it,
    because a bare repository does not read in-tree attributes.
    """

    def hide(repo):
        (repo / ".gitattributes").write_text("*.py -diff\n")
        (repo / "feature.py").write_text("def added():\n    return 2\n")

    add_to_pull_head(git_remote, hide)

    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        # Scoped to the file the attribute targets: the fixture also adds a
        # genuinely binary blob, which git is right to render this way.
        assert "Binary files a/feature.py" not in checkout.diff
        assert "def added():" in checkout.diff


async def test_only_https_is_allowed(git_remote, tmp_path, monkeypatch):
    """`GIT_ALLOW_PROTOCOL` is the control, and it is a constant.

    `file://` is what the fixtures would naturally have used; it is
    refused, which is exactly why the double speaks https instead of the
    whitelist being widened for the tests' convenience.
    """
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    refused = (
        f"file://{git_remote.serve_root}",
        git_remote.url.replace("https://", "http://", 1),
    )
    for url in refused:
        with pytest.raises(GitCommandError, match="not allowed"):
            await run_git("ls-remote", url, cwd=tmp_path)

    # The https double, by contrast, answers -- so the assertions above are
    # about the protocol and not about a broken fixture.
    listed = await run_git("ls-remote", git_remote.url, cwd=tmp_path)
    assert git_remote.head_sha in listed
