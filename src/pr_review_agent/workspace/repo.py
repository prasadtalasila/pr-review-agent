"""One bare mirror per repository, a detached worktree per run.

The mirror carries the full commit graph, so ``git merge-base`` is always
answerable and the second review of the day fetches almost nothing. Two
concurrent runs are two worktrees over one object store, which is git's
designed use -- and the run directories are **siblings** of the mirror,
because ``$GIT_DIR/worktrees`` is where git keeps each worktree's own
administrative files and a working tree placed there serves two roles at
once.

The only shared mutable state is the mirror's ref namespace, and one
``asyncio.Lock`` serialises every write to it -- the fetch and the
teardown's ref deletion alike, since ``update-ref -d`` and a concurrent
fetch contend for ``packed-refs.lock``. One process-wide lock is sufficient
rather than merely convenient, because the daemon is a single process: the
same assumption the queue's per-PR lease already rests on.

The diff is computed **in the bare mirror**, never in the worktree.
``git diff`` honours the ``.gitattributes`` of the tree it runs in, so a
pull request that adds ``*.py -diff`` renders its own changes as "Binary
files differ" while the API's addition count still looks normal. That is a
content-hiding attack on the review itself, and it needs no execution at
all; a bare repository does not read in-tree attributes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .gitcmd import WorkspaceError, run_git

logger = logging.getLogger(__name__)

GITHUB_BASE = "https://github.com"

#: Run-scoped refs live under this prefix, so a sweep can recognise one left
#: behind by a crash without guessing.
RUN_REF_PREFIX = "refs/run"


@dataclass(frozen=True)
class PullRequestFacts:
    """What a checkout must know before it touches the disk.

    All of it comes from one ``GET /repos/{owner}/{name}/pulls/{n}``, which
    is also the read that resolves ``head_sha`` for a mention trigger --
    whose payload carries none.
    """

    number: int
    head_sha: str
    base_ref: str
    additions: int
    deletions: int
    changed_files: int

    @property
    def changed_lines(self) -> int:
        """Added plus deleted: what the line cap is measured against."""
        return self.additions + self.deletions


@dataclass(frozen=True)
class Checkout:
    """An untrusted tree on disk, and the diff that describes it."""

    path: Path
    head_sha: str
    merge_base: str
    diff: str


class PullRequestTooLarge(WorkspaceError):
    """A size cap fired, before anything was written to disk."""

    def __init__(self, cap: str, observed: int, limit: int) -> None:
        self.cap = cap
        self.observed = observed
        self.limit = limit
        super().__init__(f"{cap}: {observed} exceeds the configured {limit}")


class Workspace:
    """The checkout cache for one repository."""

    def __init__(
        self, repo: str, cache_dir: Path | str, base_url: str = GITHUB_BASE
    ) -> None:
        self.repo = repo
        self.cache_dir = Path(cache_dir)
        # Not a safety knob: the https-only whitelist applies whatever this
        # is, so pointing it elsewhere cannot widen a protection. A GitHub
        # Enterprise host is a real deployment, and this is the same shape
        # as the client's configurable API base.
        self.base_url = base_url
        self._lock = asyncio.Lock()

    @property
    def mirror(self) -> Path:
        """The bare mirror for this repository."""
        return self.cache_dir / f"{self.repo.replace('/', '__')}.git"

    @property
    def runs(self) -> Path:
        """Where per-run worktrees live: beside the mirror, never inside it."""
        return self.cache_dir / "runs"

    @property
    def remote_url(self) -> str:
        """The anonymous https URL the mirror fetches from.

        No credential reaches git -- not here, not on the argv, not in
        ``.git/config``. The target repository is public, so the poller's
        token buys nothing, and not passing it is one fewer way to leak it.
        """
        return f"{self.base_url.rstrip('/')}/{self.repo}.git"

    async def run_refs(self) -> list[str]:
        """Every run-scoped ref currently in the mirror."""
        if not self.mirror.exists():
            return []
        out = await run_git(
            "-C",
            str(self.mirror),
            "for-each-ref",
            "--format=%(refname)",
            RUN_REF_PREFIX,
        )
        return out.split()

    async def sweep(self) -> None:
        """Clear what a crashed run left behind.

        A crash between ``worktree add`` and teardown leaves a worktree and
        a run-scoped ref forever. Startup is also the only safe moment to
        remove a stale lock file: mid-flight, a lock might belong to a live
        process, while at startup no git of ours is running.
        """
        if not self.mirror.exists():
            return
        for lock in self.mirror.rglob("*.lock"):
            lock.unlink(missing_ok=True)
            logger.warning("removed a stale git lock file: %s", lock)
        if self.runs.exists():
            shutil.rmtree(self.runs, ignore_errors=True)
        await run_git("-C", str(self.mirror), "worktree", "prune")
        for ref in await self.run_refs():
            await run_git("-C", str(self.mirror), "update-ref", "-d", ref)

    @asynccontextmanager
    async def checkout(
        self,
        facts: PullRequestFacts,
        *,
        max_changed_files: int,
        max_changed_lines: int,
    ) -> AsyncIterator[Checkout]:
        """Check the pull request head out, and take it away afterwards.

        The caps are arguments rather than state because ``budget`` is
        reloaded on ``SIGHUP``: a workspace holding a snapshot taken at
        construction would silently ignore a tightened cap, which is the
        exact failure the reload mechanism exists to prevent.
        """
        self._gate(facts, max_changed_files, max_changed_lines)

        run_id = uuid.uuid4().hex[:12]
        ref = f"{RUN_REF_PREFIX}/{run_id}"
        run_path = self.runs / run_id

        head_sha = await self._fetch(facts, ref)
        merge_base = (
            await run_git(
                "-C",
                str(self.mirror),
                "merge-base",
                f"refs/heads/{facts.base_ref}",
                head_sha,
            )
        ).strip()
        diff = await run_git(
            "-C", str(self.mirror), "diff", "--no-ext-diff", merge_base, head_sha
        )
        self.runs.mkdir(parents=True, exist_ok=True)
        await run_git(
            "-C",
            str(self.mirror),
            "worktree",
            "add",
            "--detach",
            str(run_path),
            head_sha,
        )
        try:
            yield Checkout(
                path=run_path, head_sha=head_sha, merge_base=merge_base, diff=diff
            )
        finally:
            await self._teardown(run_path, ref)

    @staticmethod
    def _gate(
        facts: PullRequestFacts, max_changed_files: int, max_changed_lines: int
    ) -> None:
        """Refuse an oversized pull request before the first git invocation.

        Raising here rather than after the fetch is what makes "refused
        before anything is written to disk" literally true. Note what it
        does *not* do: it bounds what the engine reads, not the volume,
        because a fetch pulls every object reachable from the head.
        """
        if facts.changed_files > max_changed_files:
            raise PullRequestTooLarge(
                "max_changed_files", facts.changed_files, max_changed_files
            )
        if facts.changed_lines > max_changed_lines:
            raise PullRequestTooLarge(
                "max_changed_lines", facts.changed_lines, max_changed_lines
            )

    async def _fetch(self, facts: PullRequestFacts, ref: str) -> str:
        """Fetch the head and the base branch; return the sha actually fetched.

        ``refs/pull/{n}/head`` lives in the base repository for forks and
        branches alike, so a fork is not a special case here.

        Both refspecs are forced. Without the ``+`` on the base branch, a
        force-push upstream makes the fetch fail non-fast-forward, and then
        every review of every pull request on that base fails until an
        operator intervenes.
        """
        async with self._lock:
            if not self.mirror.exists():
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self.cache_dir.chmod(0o700)
                await run_git("init", "--bare", "-q", str(self.mirror))
            await run_git(
                "-C",
                str(self.mirror),
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                self.remote_url,
                f"+refs/pull/{facts.number}/head:{ref}",
                f"+refs/heads/{facts.base_ref}:refs/heads/{facts.base_ref}",
            )
            # The fetched sha, not the one the API reported: the head can
            # move between the two reads, and a review has to name the
            # commit it actually read.
            out = await run_git("-C", str(self.mirror), "rev-parse", ref)
        return out.strip()

    async def _teardown(self, run_path: Path, ref: str) -> None:
        """Best-effort, and loud about it.

        A checkout that cannot be removed is a disk leak, so the operator is
        told rather than the failure being swallowed -- but the original
        exception, if there was one, is what the caller should see.
        """
        try:
            await run_git(
                "-C",
                str(self.mirror),
                "worktree",
                "remove",
                "--force",
                str(run_path),
            )
            async with self._lock:
                await run_git("-C", str(self.mirror), "update-ref", "-d", ref)
        except WorkspaceError:
            logger.warning(
                "could not tear down %s; it is leaking disk until the next sweep",
                run_path,
                exc_info=True,
            )
