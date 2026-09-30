"""Incremental rounds: what a later round is shown, and when it falls back.

Every case is a real push against the loopback remote -- a fixup, an amend,
a rebase, a merge from the base -- because the whole point of comparing
trees is that force-pushes are ordinary, and a stubbed sha would test none
of that.
"""

import logging
import sys

import pytest
from conftest import PR_NUMBER, VENDORED_LINES

from pr_review_agent.workspace import PullRequestFacts, PullRequestTooLarge, since
from pr_review_agent.workspace.gitcmd import GitCommandError

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; Git for Windows differs",
)

CAPS = {"max_changed_files": 100, "max_changed_lines": 5000}
VENDORED = ("**/vendor/**",)


def facts(head_sha):
    return PullRequestFacts(
        number=PR_NUMBER,
        head_sha=head_sha,
        base_ref="main",
        additions=2,
        deletions=0,
        changed_files=1,
        state="open",
    )


async def first_round(workspace, git_remote):
    """Review the pull request once, so the mirror holds its head."""
    async with workspace.checkout(facts(git_remote.head_sha), **CAPS) as checkout:
        return checkout.head_sha


async def later_round(workspace, head, since_sha, **kwargs):
    async with workspace.checkout(
        facts(head), since_sha=since_sha, **{**CAPS, **kwargs}
    ) as checkout:
        return checkout


def changed(checkout):
    """The paths the diff shows."""
    return {
        line.split(" b/", 1)[1]
        for line in checkout.diff.splitlines()
        if line.startswith("diff --git")
    }


# -- incremental ---------------------------------------------------------


async def test_a_fixup_commit_is_the_whole_diff(workspace, git_remote, contributor):
    old = await first_round(workspace, git_remote)
    head = contributor.commit("fixup.py", "x = 1\n")

    async with workspace.checkout(facts(head), since_sha=old, **CAPS) as checkout:
        # The tree on disk is still the whole head, not just the fixup.
        assert (checkout.path / "feature.py").exists()

    assert checkout.since_sha == old
    assert changed(checkout) == {"fixup.py"}
    assert (checkout.reviewed.files, checkout.reviewed.lines) == (1, 1)


async def test_an_amend_shows_only_what_the_amend_changed(
    workspace, git_remote, contributor
):
    old = await first_round(workspace, git_remote)
    head = contributor.commit("feature.py", "def added():\n    return 2\n", amend=True)

    checkout = await later_round(workspace, head, old)

    assert checkout.since_sha == old
    assert changed(checkout) == {"feature.py"}
    assert checkout.reviewed.lines == 2


async def test_a_force_push_that_changes_nothing_has_nothing_to_review(
    workspace, git_remote, contributor
):
    old = await first_round(workspace, git_remote)
    head = contributor.reword()

    checkout = await later_round(workspace, head, old)

    assert head != old
    assert checkout.since_sha == old
    assert (checkout.reviewed.files, checkout.reviewed.lines) == (0, 0)


async def test_a_rebase_onto_a_moved_base_hides_the_upstream_commits(
    workspace, git_remote, contributor
):
    old = await first_round(workspace, git_remote)
    contributor.advance_base("upstream.txt", "landed elsewhere\n")
    contributor.rebase()
    head = contributor.commit("fixup.py", "x = 1\n")

    checkout = await later_round(workspace, head, old)

    assert checkout.since_sha == old
    assert changed(checkout) == {"fixup.py"}


async def test_a_merge_from_the_base_hides_the_upstream_commits(
    workspace, git_remote, contributor
):
    old = await first_round(workspace, git_remote)
    contributor.advance_base("upstream.txt", "landed elsewhere\n")
    head = contributor.merge_base_in()

    checkout = await later_round(workspace, head, old)

    assert checkout.since_sha == old
    assert changed(checkout) == set()


async def test_exclusions_apply_to_the_incremental_range(
    workspace, git_remote, contributor
):
    old = await first_round(workspace, git_remote)
    head = contributor.commit("vendor/lib.js", "var y = 1;\n")

    checkout = await later_round(workspace, head, old, excluded_paths=VENDORED)

    assert checkout.since_sha == old
    assert checkout.reviewed.lines == 0


# -- the size gate measures the range it shows (CLAUDE.md section 5) -----


async def test_a_small_fixup_on_an_oversized_pull_request_is_reviewed(
    workspace, git_remote, contributor
):
    """The widening, and its bound: the cap now applies to the range shown."""
    old = await first_round(workspace, git_remote)
    head = contributor.commit("fixup.py", "x = 1\n")
    cap = {"max_changed_lines": 10}
    assert cap["max_changed_lines"] < VENDORED_LINES

    with pytest.raises(PullRequestTooLarge):
        await later_round(workspace, head, None, **cap)
    checkout = await later_round(workspace, head, old, **cap)
    assert checkout.reviewed.lines == 1


async def test_a_fixup_larger_than_the_cap_is_still_refused(
    workspace, git_remote, contributor
):
    old = await first_round(workspace, git_remote)
    head = contributor.commit("fixup.py", "x = 1\n" * 11)

    with pytest.raises(PullRequestTooLarge) as refused:
        await later_round(workspace, head, old, max_changed_lines=10)
    assert refused.value.observed == 11


# -- full, and why -------------------------------------------------------


async def test_a_first_round_is_full(workspace, git_remote):
    checkout = await later_round(workspace, git_remote.head_sha, None)

    assert checkout.since_sha is None
    assert "feature.py" in changed(checkout)


async def test_an_unmoved_head_is_reviewed_in_full(workspace, git_remote):
    old = await first_round(workspace, git_remote)

    checkout = await later_round(workspace, old, old)

    assert checkout.since_sha is None
    assert "feature.py" in changed(checkout)


async def test_a_previous_head_the_mirror_never_had_is_full_and_logged(
    workspace, git_remote, caplog
):
    missing = "0" * 40
    caplog.set_level(logging.INFO, logger=since.__name__)

    checkout = await later_round(workspace, git_remote.head_sha, missing)

    assert checkout.since_sha is None
    assert "feature.py" in changed(checkout)
    assert missing in caplog.text


async def test_a_replay_that_conflicts_is_full(
    workspace, git_remote, contributor, caplog
):
    caplog.set_level(logging.INFO, logger=since.__name__)
    old = await first_round(workspace, git_remote)
    # Upstream adds the same file the pull request adds, differently; the
    # contributor resolves it by rebasing with their own side.
    contributor.advance_base("feature.py", "def upstream():\n    return 0\n")
    head = contributor.rebase("-X", "theirs")

    checkout = await later_round(workspace, head, old)

    assert checkout.since_sha is None
    assert "feature.py" in changed(checkout)
    assert "conflict with the new base" in caplog.text


async def test_a_git_that_cannot_replay_falls_back_and_says_so_once(
    workspace, git_remote, contributor, monkeypatch, caplog
):
    """Below 2.38 a rebase is full; a fixup is still incremental."""
    old = await first_round(workspace, git_remote)
    contributor.advance_base("upstream.txt", "landed elsewhere\n")
    rebased = contributor.rebase()

    async def git_2_37():
        return (2, 37)

    monkeypatch.setattr(since, "git_version", git_2_37)
    monkeypatch.setattr(since, "_can_replay", None)
    caplog.set_level(logging.INFO, logger=since.__name__)

    for _ in range(2):
        checkout = await later_round(workspace, rebased, old)
        assert checkout.since_sha is None
    fixup = contributor.commit("fixup.py", "x = 1\n")
    assert (await later_round(workspace, fixup, rebased)).since_sha == rebased
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "2.38" in warnings[0].getMessage()


async def test_a_replay_that_fails_outright_is_an_error_and_full(
    workspace, git_remote, contributor, monkeypatch, caplog
):
    old = await first_round(workspace, git_remote)
    contributor.advance_base("upstream.txt", "landed elsewhere\n")
    head = contributor.rebase()
    real = since.run_git

    async def timing_out(*args, **kwargs):
        if "merge-tree" in args:
            raise GitCommandError(args, -1, "timed out after 300s")
        return await real(*args, **kwargs)

    monkeypatch.setattr(since, "run_git", timing_out)

    checkout = await later_round(workspace, head, old)

    assert checkout.since_sha is None
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize(("fixups", "incremental"), [(1, False), (2, True)])
async def test_below_the_commit_threshold_the_round_is_full(
    workspace, git_remote, contributor, fixups, incremental
):
    old = await first_round(workspace, git_remote)
    head = old
    for index in range(fixups):
        head = contributor.commit(f"fixup{index}.py", "x = 1\n")

    checkout = await later_round(workspace, head, old, min_commits=2)

    assert (checkout.since_sha == old) is incremental
