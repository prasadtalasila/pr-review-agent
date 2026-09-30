"""Where an incremental round's diff starts, or that it has to be full.

Roadmap C2. A later round on a pull request only needs to be shown what
changed since the head the previous completed round read. The question is
answered by comparing **trees, not history**, which is what makes a
force-push an ordinary case rather than a reason to start over: the
reviewer never read the commits, it read the old head's content, and
``git diff <old head> <new head>`` compares two contents whatever the graph
between them looks like. An amend, a squash, a reword or a reorder all
diff cleanly, and a squash that changed nothing diffs to nothing.

**What does break a tree diff is the base moving under it.** A rebase onto
a newer base, or a merge of the base into the branch, carries upstream
commits into the new head, and a plain diff against the old head would show
them as the pull request's own. So when the merge base has moved, the old
head's changes are first replayed onto the new merge base with
``git merge-tree --write-tree``, and the diff starts from that tree instead.
What remains is exactly what the contributor changed beyond the rebase.

Replaying cannot hide anything the contributor wrote. A line that reads the
same in the replayed tree and the new head is either the old head's own
content, which the previous round reviewed, or the new base's, which is not
the pull request's. Everything else shows.

**Every other answer is a full round.** A first round, an unmoved head, a
previous head the mirror no longer holds, a replay that conflicts, and a
git too old to replay at all (``--write-tree`` arrived in 2.38; the floor
is 2.32). None of them is refused and none of them diffs against nothing:
they fall back to ``merge_base..head`` and say why in the log.

Runs in the bare mirror, like every diff this package computes, so no
in-tree ``.gitattributes`` or merge driver is consulted; the config that
could name a driver is nulled by :func:`gitcmd.git_environment`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .gitcmd import GitCommandError, git_version, run_git

logger = logging.getLogger(__name__)

#: The first git whose ``merge-tree`` has ``--write-tree``. Below it the
#: old three-argument form reads ``--write-tree`` as a revision name and
#: exits 128, the same code as a real failure, so the version is asked
#: rather than the exit code read.
REPLAY_GIT_VERSION = (2, 38)

#: How ``git merge-tree --write-tree`` and ``git rev-parse --verify --quiet``
#: say "conflict" and "no such commit": an answer, not a failure.
_ANSWERED_NO = 1

#: Whether this process's git can replay, asked once. ``None`` until then.
_can_replay: bool | None = None


async def diff_start(
    mirror: Path,
    *,
    base_ref: str,
    merge_base: str,
    head_sha: str,
    since_sha: str,
    min_commits: int = 0,
) -> str | None:
    """The object an incremental diff starts from, or ``None`` for full.

    The answer is a commit when the merge base has not moved, and a tree
    when it has. ``git diff`` takes either, so the caller need not care.

    A missing commit or a history with no common base is an answer and
    means a full round. Any other git failure is a failure, and raises like
    every other step of the checkout.
    """
    # Keyword-only, one per fact about the round. pylint: disable=too-many-arguments
    if since_sha == head_sha:
        return _full(since_sha, "the head has not moved since that round")
    held = f"{since_sha}^{{commit}}"
    if await _answer(mirror, "rev-parse", "--verify", "--quiet", held) is None:
        return _full(since_sha, "the mirror no longer holds it")
    old_base = await _answer(mirror, "merge-base", f"refs/heads/{base_ref}", since_sha)
    if old_base is None:
        return _full(since_sha, "it shares no history with the base")
    if min_commits:
        count = await _git(mirror, "rev-list", "--count", f"{since_sha}..{head_sha}")
        if int(count) < min_commits:
            return _full(since_sha, f"fewer than {min_commits} new commit(s)")
    if old_base == merge_base:
        return since_sha
    if not await _replay_supported():
        return _full(since_sha, "the base moved and this git cannot replay it")
    return await _replay(mirror, merge_base, since_sha)


async def _replay(mirror: Path, merge_base: str, since_sha: str) -> str | None:
    """The previous head's changes re-applied to ``merge_base``, as a tree.

    A conflict is an answer, and means a full round. Any other failure is
    logged at ERROR and *also* means a full round rather than a failed
    review: a replay that always times out on one pull request would
    otherwise stop that pull request being reviewed at all.
    """
    try:
        out = await _git(mirror, "merge-tree", "--write-tree", merge_base, since_sha)
    except GitCommandError as exc:
        if exc.returncode == _ANSWERED_NO:
            return _full(since_sha, "its changes conflict with the new base")
        logger.error("could not replay %s onto %s: %s", since_sha, merge_base, exc)
        return _full(since_sha, "the replay failed")
    return out.splitlines()[0]


async def _replay_supported() -> bool:
    """Whether this git has ``merge-tree --write-tree``, said once if not."""
    global _can_replay  # pylint: disable=global-statement
    if _can_replay is None:
        _can_replay = await git_version() >= REPLAY_GIT_VERSION
        if not _can_replay:
            logger.warning(
                "git is older than %d.%d, so a rebased pull request is reviewed "
                "in full; fixups and amends are still incremental",
                *REPLAY_GIT_VERSION,
            )
    return _can_replay


async def _answer(mirror: Path, *args: str) -> str | None:
    """A git answer, or ``None`` when git answered "no" with exit 1."""
    try:
        return await _git(mirror, *args)
    except GitCommandError as exc:
        if exc.returncode == _ANSWERED_NO:
            return None
        raise


def _full(since_sha: str, reason: str) -> None:
    """Say why this round covers the whole pull request, and return ``None``."""
    logger.info("reviewing the whole pull request, not since %s: %s", since_sha, reason)


async def _git(mirror: Path, *args: str) -> str:
    return (await run_git("-C", str(mirror), *args)).strip()
