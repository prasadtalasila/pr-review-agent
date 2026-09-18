"""One bare mirror, a detached worktree per run, and nothing left behind."""

import asyncio
import sys

import pytest
from conftest import PR_NUMBER, git

from pr_review_agent.workspace import (
    PullRequestFacts,
    PullRequestTooLarge,
    Workspace,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)

CAPS = {"max_changed_files": 100, "max_changed_lines": 5000}


def facts(remote, **overrides) -> PullRequestFacts:
    values = {
        "number": PR_NUMBER,
        "head_sha": remote.head_sha,
        "base_ref": remote.base_ref,
        "additions": 2,
        "deletions": 0,
        "changed_files": 1,
    }
    values.update(overrides)
    return PullRequestFacts(**values)


@pytest.fixture
def workspace(git_remote, tmp_path, monkeypatch) -> Workspace:
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    return Workspace(
        repo=git_remote.repo,
        cache_dir=tmp_path / "cache",
        base_url=git_remote.base_url,
    )


async def test_a_fork_shaped_head_is_checked_out_at_its_exact_sha(
    workspace, git_remote
):
    # The fixture's head is reachable only from refs/pull/7/head, which is
    # what a fork looks like -- and it takes no special code path.
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert checkout.head_sha == git_remote.head_sha
        assert (checkout.path / "feature.py").read_text().startswith("def added")


async def test_the_worktree_is_detached_at_that_commit(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        head = (checkout.path / ".git").read_text()
        assert "gitdir:" in head  # a worktree, not a clone
        assert checkout.path.parent.name == "runs"


async def test_the_run_directory_is_not_inside_the_mirror(workspace, git_remote):
    # $GIT_DIR/worktrees is git's own administrative area; a working tree
    # placed there serves two roles and reports git's files as untracked.
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert workspace.mirror not in checkout.path.parents


async def test_the_diff_is_against_the_merge_base(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert "def added():" in checkout.diff
        assert checkout.merge_base != checkout.head_sha
        # base.txt is on both sides of the merge base, so it must not show.
        assert "base.txt" not in checkout.diff


async def test_head_sha_is_resolved_when_the_trigger_carried_none(
    workspace, git_remote
):
    # A mention trigger's queue row has head_sha NULL. The facts read is
    # what fills it, and the fetched sha is what the checkout reports --
    # not the one the API happened to report a moment earlier.
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert len(checkout.head_sha) == 40
        assert checkout.head_sha == git_remote.head_sha


async def test_too_many_changed_lines_is_refused_before_any_disk_write(
    workspace, git_remote, tmp_path
):
    with pytest.raises(PullRequestTooLarge, match="max_changed_lines"):
        async with workspace.checkout(facts(git_remote, additions=9000), **CAPS):
            pass
    assert not (tmp_path / "cache").exists()


async def test_too_many_changed_files_is_refused_before_any_disk_write(
    workspace, git_remote, tmp_path
):
    with pytest.raises(PullRequestTooLarge, match="max_changed_files"):
        async with workspace.checkout(facts(git_remote, changed_files=9000), **CAPS):
            pass
    assert not (tmp_path / "cache").exists()


async def test_the_refusal_names_the_cap_and_the_numbers(workspace, git_remote):
    with pytest.raises(PullRequestTooLarge) as excinfo:
        async with workspace.checkout(
            facts(git_remote, additions=4000, deletions=4000), **CAPS
        ):
            pass
    assert excinfo.value.cap == "max_changed_lines"
    assert excinfo.value.observed == 8000
    assert excinfo.value.limit == 5000


async def test_a_pull_request_exactly_on_the_cap_is_allowed(workspace, git_remote):
    async with workspace.checkout(
        facts(git_remote, additions=5000, deletions=0, changed_files=100), **CAPS
    ) as checkout:
        assert checkout.path.exists()


async def test_teardown_removes_the_worktree_and_the_run_ref(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        path = checkout.path
        assert path.exists()
    assert not path.exists()
    assert await workspace.run_refs() == []


async def test_a_repeated_run_returns_to_the_same_baseline(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    after_first = sorted(p.name for p in workspace.runs.iterdir())
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    assert sorted(p.name for p in workspace.runs.iterdir()) == after_first == []
    assert await workspace.run_refs() == []


async def test_two_concurrent_checkouts_do_not_interfere(workspace, git_remote):
    seen = []

    async def once() -> None:
        async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
            seen.append(checkout.path)
            # Hold both trees open at once, so the assertion below is about
            # two live worktrees rather than two sequential ones.
            await asyncio.sleep(0.1)
            assert (checkout.path / "feature.py").exists()

    await asyncio.gather(once(), once())
    assert len(set(seen)) == 2
    assert await workspace.run_refs() == []


async def test_no_credential_reaches_the_wire(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    sent = {key.lower() for request in git_remote.requests for key in request}
    assert "authorization" not in sent
    assert "proxy-authorization" not in sent


async def test_the_mirror_keeps_no_remote_url(workspace, git_remote):
    # Nothing about the remote persists to disk, so nothing can leak from
    # .git/config later.
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    assert "127.0.0.1" not in (workspace.mirror / "config").read_text()


async def test_the_second_run_refetches_into_the_same_mirror(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    first = workspace.mirror
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    assert workspace.mirror == first
    assert len(list(workspace.cache_dir.glob("*.git"))) == 1


async def test_a_force_pushed_base_branch_still_fetches(workspace, git_remote):
    """Both refspecs are forced.

    Without the `+` on the base branch, a force-push upstream makes the
    fetch fail non-fast-forward -- and then every review of every pull
    request on that base fails until an operator intervenes.

    The rewrite here is the realistic one: the base branch moves to a
    sibling commit, so it is no longer a descendant of what the mirror
    holds, while still sharing a merge base with the pull request.
    """
    serve = git_remote.serve_root
    root = git("rev-parse", "refs/heads/main", cwd=serve)
    tree = git("rev-parse", "refs/heads/main^{tree}", cwd=serve)

    advanced = git("commit-tree", "-m", "advanced", "-p", root, tree, cwd=serve)
    git("update-ref", "refs/heads/main", advanced, cwd=serve)
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass

    # A sibling of `advanced`, so the mirror's copy of main is not an
    # ancestor of it: exactly what a force-push looks like from here.
    rewritten = git("commit-tree", "-m", "rewritten", "-p", root, tree, cwd=serve)
    assert rewritten != advanced
    git("update-ref", "refs/heads/main", rewritten, cwd=serve)

    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert checkout.head_sha == git_remote.head_sha
        assert "def added():" in checkout.diff


# -- the startup sweep ---------------------------------------------------


async def test_the_sweep_clears_what_a_crash_left_behind(workspace, git_remote):
    # Enter the context manager and never exit it: that is what a crash
    # between `worktree add` and teardown leaves on disk.
    manager = workspace.checkout(facts(git_remote), **CAPS)
    checkout = await manager.__aenter__()
    assert checkout.path.exists()
    assert await workspace.run_refs() != []

    await workspace.sweep()

    assert not checkout.path.exists()
    assert await workspace.run_refs() == []


async def test_the_sweep_removes_a_stale_lock(workspace, git_remote):
    """A SIGKILLed git leaves a lock that wedges every later fetch.

    Startup is the only safe moment to clear one: mid-flight the lock might
    belong to a live process.
    """
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    stale = workspace.mirror / "refs" / "stale.lock"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("")

    await workspace.sweep()

    assert not stale.exists()


async def test_the_sweep_is_a_no_op_before_the_first_fetch(workspace):
    await workspace.sweep()
    assert not workspace.mirror.exists()
