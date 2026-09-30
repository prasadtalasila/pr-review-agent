"""One bare mirror per repository, a detached worktree per run.

The mirror carries the full commit graph, so ``git merge-base`` is always
answerable and the second review of the day fetches almost nothing. Two
concurrent runs are two worktrees over one object store, which is git's
designed use -- and the run directories are **siblings** of the mirror,
because ``$GIT_DIR/worktrees`` is where git keeps each worktree's own
administrative files and a working tree placed there serves two roles at
once. The placement is held by the path being absolute: ``git -C`` resolves
a relative argument against the mirror, which would put the run directory
inside the very ``$GIT_DIR`` this rules out.

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

from .exclusions import omitted, pathspec
from .gitcmd import WorkspaceError, run_git
from .since import diff_start

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
    whose payload carries none. ``state`` and ``merged`` ride along on that
    same read, so asking whether the checkout is worth making at all costs
    no extra request.
    """

    # One payload, one record of it. Splitting the state fields off into a
    # second type would put the question "is this worth reviewing" and the
    # answer "here is what to check out" in two places, when they arrive in
    # the same response and are read three lines apart.
    # pylint: disable=too-many-instance-attributes

    number: int
    head_sha: str
    base_ref: str
    additions: int
    deletions: int
    changed_files: int
    state: str
    merged: bool = False

    @property
    def changed_lines(self) -> int:
        """Added plus deleted: what the line cap is measured against."""
        return self.additions + self.deletions

    @property
    def is_open(self) -> bool:
        """Whether reviewing this pull request can still be read by anybody.

        Carried here because this read is the *only* moment between a
        trigger being queued and the money being spent at which GitHub is
        asked about the pull request at all. A trigger queued while the
        pull request was open waits its turn in the queue, and by the time
        a worker claims it the pull request may have been merged or
        closed -- at which point the review costs the same and nobody will
        read it.

        ``merged`` is checked as well as ``state`` because the two are
        answered by different fields: a merged pull request reads
        ``closed`` today, but the flag is what says so without depending on
        that.
        """
        return self.state == "open" and not self.merged


@dataclass(frozen=True)
class DiffSize:
    """How much there is to review, once exclusions have been applied.

    Not the same numbers as :class:`PullRequestFacts`, and deliberately so.
    Those are the API's totals over every changed path; these are what
    survived ``budget.excluded_paths``, which is both what the size gate
    counts and what the engine will be shown.
    """

    files: int
    lines: int

    @classmethod
    def from_numstat(cls, numstat: str) -> DiffSize:
        """Count ``git diff --numstat`` output.

        One row per changed file: ``added``, ``deleted``, ``path``. A binary
        file reports ``-`` for both counts, and is one file of zero lines.

        The path is never parsed -- only the row count and the first two
        columns are needed -- so a path containing a newline, which git
        quotes, cannot confuse the count.
        """
        files = lines = 0
        for row in numstat.splitlines():
            if not row.strip():
                continue
            files += 1
            added, deleted = row.split("\t", 2)[:2]
            if added != "-":
                lines += int(added) + int(deleted)
        return cls(files=files, lines=lines)


@dataclass(frozen=True)
class Checkout:
    """An untrusted tree on disk, and the diff that describes it.

    ``since_sha`` is set when the diff covers only what changed since the
    head a previous round reviewed, and ``None`` when it covers the whole
    pull request from ``merge_base``. ``reviewed`` always measures the diff
    actually shown, whichever it is. The tree on disk is the whole head
    either way, so a finding outside the diff can still be checked.

    ``omitted`` is what ``excluded_paths`` withheld from that same diff, as
    :func:`.exclusions.omitted` groups it, so the review can say what it did
    not read.
    """

    path: Path
    head_sha: str
    merge_base: str
    diff: str
    reviewed: DiffSize
    since_sha: str | None = None
    omitted: tuple[tuple[str, int], ...] = ()


class PullRequestTooLarge(WorkspaceError):
    """A size cap fired, before a worktree was created.

    ``observed`` is the reviewable figure -- after ``excluded_paths`` -- and
    the message also reports the API's own totals, because "refused at 120
    files" is baffling next to a pull request GitHub says has 900. The gap
    between the two numbers *is* the explanation.

    That explanation is owed to the contributor as much as to the operator,
    which is what :attr:`notice` is for.
    """

    def __init__(
        self, cap: str, observed: int, limit: int, facts: PullRequestFacts
    ) -> None:
        self.cap = cap
        self.observed = observed
        self.limit = limit
        self.facts = facts
        super().__init__(
            f"{cap}: {observed} exceeds the configured {limit} "
            f"(the pull request reports {facts.changed_files} files, "
            f"{facts.changed_lines} lines, before exclusions)"
        )

    @property
    def notice(self) -> str:
        """The same refusal, addressed to whoever asked for the review.

        A sibling of the log message rather than a second source of truth:
        both read the same four fields, so the number an operator sees in
        the journal is the number the pull request is told. What differs is
        only what each reader can do about it, so this one names the setting
        to change.

        No markup beyond backticks and the emphasis the publisher adds. It
        is assembled from a cap name this process chose and three integers,
        with no engine prose anywhere in it -- which is why the escaping
        :mod:`pr_review_agent.sanitise` does for a review is not needed here
        and would have nothing to bite on.
        """
        return (
            f"`{self.cap}`: {self.observed} exceeds the configured "
            f"{self.limit}, counting only what survives "
            f"`budget.excluded_paths`. The pull request itself reports "
            f"{self.facts.changed_files} changed files and "
            f"{self.facts.changed_lines} changed lines."
        )


class Workspace:
    """The checkout cache for one repository."""

    def __init__(
        self, repo: str, cache_dir: Path | str, base_url: str = GITHUB_BASE
    ) -> None:
        self.repo = repo
        # Absolute, once, here. Every git command below is `git -C <mirror>`,
        # which is a chdir -- so a relative path on that argv is resolved
        # against the mirror and the worktree lands inside $GIT_DIR, while
        # `Checkout.path` still reads as relative to the daemon's own working
        # directory and the engine is handed a cwd that does not exist. The
        # shipped default is relative, so this is the configured case rather
        # than an exotic one.
        self.cache_dir = Path(cache_dir).resolve()
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
        excluded_paths: tuple[str, ...] = (),
        since_sha: str | None = None,
        min_commits: int = 0,
    ) -> AsyncIterator[Checkout]:
        # Every keyword is a reloadable setting or a round's input, passed per
        # call for the reason the docstring gives; bundling them would only
        # move the list. pylint: disable=too-many-arguments,too-many-locals
        """Check the pull request head out, and take it away afterwards.

        The caps are arguments rather than state because ``budget`` is
        reloaded on ``SIGHUP``: a workspace holding a snapshot taken at
        construction would silently ignore a tightened cap, which is the
        exact failure the reload mechanism exists to prevent.
        ``excluded_paths`` travels with them for the same reason.

        The gate fires after the fetch rather than before it, because
        exclusions cannot be subtracted from the API's three aggregate
        integers -- see ``_gate``.

        ``since_sha`` is the head the previous completed round reviewed.
        When :func:`since.diff_start` finds a safe place to start from, the
        size gate and the diff both measure from there -- one variable feeds
        both, so they cannot disagree -- and otherwise the round is full.
        ``min_commits`` is ``budget.incremental_min_commits``.
        """
        run_id = uuid.uuid4().hex[:12]
        ref = f"{RUN_REF_PREFIX}/{run_id}"
        run_path = self.runs / run_id

        head_sha = await self._fetch(facts, ref)
        # Everything between the fetch and the worktree now has a routine
        # way to fail -- the gate -- rather than only a crashing one, and a
        # refused pull request must not leave its ref behind for the next
        # startup sweep to find.
        try:
            merge_base = (
                await run_git(
                    "-C",
                    str(self.mirror),
                    "merge-base",
                    f"refs/heads/{facts.base_ref}",
                    head_sha,
                )
            ).strip()
            start = since_sha and await diff_start(
                self.mirror,
                base_ref=facts.base_ref,
                merge_base=merge_base,
                head_sha=head_sha,
                since_sha=since_sha,
                min_commits=min_commits,
            )
            span = (start or merge_base, head_sha, *pathspec(excluded_paths))
            reviewed = DiffSize.from_numstat(await self._diff("--numstat", *span))
            self._gate(facts, reviewed, max_changed_files, max_changed_lines)
            diff = await self._diff(*span)
            withheld = await self._withheld(*span[:2], excluded_paths)
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
        except BaseException:
            await self._drop_ref(ref)
            raise
        try:
            yield Checkout(
                path=run_path,
                head_sha=head_sha,
                merge_base=merge_base,
                diff=diff,
                reviewed=reviewed,
                since_sha=since_sha if start else None,
                omitted=withheld,
            )
        finally:
            await self._teardown(run_path, ref)

    async def _withheld(
        self, start: str, head_sha: str, excluded_paths: tuple[str, ...]
    ) -> tuple[tuple[str, int], ...]:
        """The changed paths ``excluded_paths`` kept out of ``start..head_sha``.

        Two name lists over the same range, with and without the pathspec,
        rather than the pathspec inverted: the grouping needs to know what
        *was* reviewed as well, to never name a directory that was. ``-z``
        because a path may hold a newline, and ``--no-renames`` so both lists
        pair a move the same way. Local git in the mirror; no engine, no
        tokens.
        """
        if not excluded_paths:
            return ()
        names = ("--name-only", "-z", "--no-renames", start, head_sha)
        changed = await self._diff(*names)
        kept = await self._diff(*names, *pathspec(excluded_paths))
        return omitted(changed.split("\0")[:-1], kept.split("\0")[:-1])

    async def _diff(self, *args: str) -> str:
        """``git diff`` in the mirror, where in-tree attributes cannot reach it."""
        return await run_git("-C", str(self.mirror), "diff", "--no-ext-diff", *args)

    @staticmethod
    def _gate(
        facts: PullRequestFacts,
        reviewed: DiffSize,
        max_changed_files: int,
        max_changed_lines: int,
    ) -> None:
        """Refuse an oversized pull request, before a worktree exists.

        Measured on what survived ``excluded_paths``, never on the API's
        totals: a vendored-dependency bump must not be refused on size for
        lines the engine will never be shown. That is also why this runs
        after the fetch rather than before it -- ``facts`` carries three
        aggregate integers with no per-path breakdown, and a lockfile cannot
        be subtracted from an integer.

        What is given up is "refused before anything is written to disk". A
        fetch costs bandwidth and disk; it costs no tokens, and these caps
        are a *spending* control. They bound what the engine reads, which
        was never the same thing as what the fetch downloads.
        """
        if reviewed.files > max_changed_files:
            raise PullRequestTooLarge(
                "max_changed_files", reviewed.files, max_changed_files, facts
            )
        if reviewed.lines > max_changed_lines:
            raise PullRequestTooLarge(
                "max_changed_lines", reviewed.lines, max_changed_lines, facts
            )

    async def _drop_ref(self, ref: str) -> None:
        """Delete one run-scoped ref, under the mirror's ref-namespace lock.

        ``update-ref -d`` and a concurrent fetch contend for
        ``packed-refs.lock``, which is why every write to the namespace --
        this one included -- is serialised.
        """
        async with self._lock:
            await run_git("-C", str(self.mirror), "update-ref", "-d", ref)

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
            await self._drop_ref(ref)
        except WorkspaceError:
            logger.error(
                "could not tear down %s; it is leaking disk until the next sweep",
                run_path,
                exc_info=True,
            )
