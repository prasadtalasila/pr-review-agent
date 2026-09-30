"""What a paid review produced, kept so publishing it can be retried.

Everything before this module is recoverable by running it again. A review is
not: the tokens are spent by the time the publisher is asked, so a GitHub
write that fails at the last step must not cost a second review to recover
from. Recording the findings *before* publishing is what makes the retry
publish-only, and it is the whole reason this table exists.

**One row per trigger, keyed the same way the queue and the ledger are.**
``dedupe_key`` joins all three, which is what turns "every posted comment is
traceable to a ledger row recording engine, model, mode, usage and
confidence" into a query rather than a convention.

**The comment id is written per run and never read back.** Each review
posts its own comment, so the id is a record of what this run produced --
traceable, and what a future line-anchored comment would need -- rather than
an address the next round writes to. Nothing dereferences it, which is why a
comment a maintainer deletes can no longer strand a paid review (issue #71).

**A publication that cannot succeed is eventually given up on.**
``publish_attempts`` counts posts tried for one run and ``publish_failed_at``
stamps the one that exhausted ``publish.max_publish_attempts``. Until this
existed a run whose post GitHub would never accept -- a locked pull request,
a repository with issues disabled -- was offered on every claim for the
lifetime of the database, because a publish-only retry deliberately does not
count against ``max_attempts``. A stamped run is no longer offered; clearing
the stamp puts it back.

**Findings are JSON text rather than a child table.** They are written once,
read once and purged wholesale; no query selects on a finding's path, line or
severity. A child table would add a migration, a join and a cascade to store
a list nobody queries into.

**A purge is not an empty review.** ``content_purged_at`` is stamped
separately from emptying ``findings``, because a run whose content was
deleted after the pull request merged has to stay distinguishable from a run
that looked and found nothing -- the same distinction ``Outcome`` keeps
between ``TRUNCATED`` and a clean empty result, and ``UsageConfidence``
keeps between ``unavailable`` and zero. A purged run is never offered for
publication: there is nothing left to post.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ._time import parse, stamp
from .engine import Finding, Outcome, ReviewResult, Severity
from .store import SqliteStore
from .triggers.models import Trigger, TriggerKind

logger = logging.getLogger(__name__)

_RECORD = """
INSERT INTO runs
    (dedupe_key, repo, pr_number, head_sha, outcome, findings, omitted,
     recorded_at)
VALUES (:key, :repo, :pr, :sha, :outcome, :findings, :omitted, :now)
ON CONFLICT(dedupe_key) DO UPDATE SET
    head_sha = :sha, outcome = :outcome, findings = :findings,
    omitted = :omitted, recorded_at = :now
"""

# One named run, if it is still waiting to be posted. `content_purged_at IS
# NULL` because a purged run has no findings left and publishing it would
# post an empty review over a real one.
_UNPUBLISHED = """
SELECT dedupe_key, repo, pr_number, head_sha, outcome, findings, comment_id,
       omitted
FROM runs
WHERE dedupe_key = :key
  AND published_at IS NULL AND content_purged_at IS NULL
  AND publish_failed_at IS NULL
"""

_MARK_PUBLISHED = """
UPDATE runs SET published_at = :now, comment_id = :comment,
                publish_outcome = :outcome
WHERE dedupe_key = :key AND published_at IS NULL
"""

# One more post tried for this run, stamped as given up on when that was the
# last one allowed. `published_at IS NULL` because a run posted by another
# worker in between is not a failure to record.
_PUBLISH_FAILED = """
UPDATE runs
SET publish_attempts = publish_attempts + 1,
    publish_failed_at = CASE
        WHEN publish_attempts + 1 >= :limit THEN :now ELSE NULL
    END
WHERE dedupe_key = :key AND published_at IS NULL
"""

_PUBLISH_FAILED_AT = """
SELECT publish_failed_at FROM runs WHERE dedupe_key = :key
"""

# `findings` is emptied rather than set NULL: the column is NOT NULL, and an
# empty list is what a reader of a purged row should see. `omitted` goes with
# it: the paths are the contributor's tree as much as the findings are.
_PURGE = """
UPDATE runs SET findings = '[]', omitted = '[]', content_purged_at = :now
WHERE repo = :repo AND pr_number = :pr AND content_purged_at IS NULL
"""


# Every completed, unpurged round for this pull request, newest first. One
# query answers both cross-round questions: the first row is what the last
# round found, and the largest number across all rows is what has been
# issued. A purged row is excluded because its findings were deleted, not
# resolved -- reusing its numbers would relabel items a reader referred to.
_HISTORY = """
SELECT findings, head_sha, recorded_at FROM runs
WHERE repo = :repo AND pr_number = :pr
  AND outcome = 'completed' AND content_purged_at IS NULL
ORDER BY recorded_at DESC, rowid DESC
"""

# Oldest first, so the position of a key in this list is its round number.
_ROUNDS = """
SELECT dedupe_key FROM runs
WHERE repo = :repo AND pr_number = :pr
  AND outcome = 'completed' AND content_purged_at IS NULL
ORDER BY recorded_at, rowid
"""


@dataclass(frozen=True)
class RecordedRun:
    """One completed review, as it was stored.

    ``omitted`` is ``Checkout.omitted`` for the diff the review was shown.
    """

    # One field per stored column the publisher reads back; a nested record
    # would only rename them. pylint: disable=too-many-instance-attributes

    dedupe_key: str
    repo: str
    pr_number: int
    head_sha: str
    outcome: Outcome
    findings: tuple[Finding, ...]
    comment_id: int | None
    omitted: tuple[tuple[str, int], ...] = ()


#: Prefixed so a publication item can never collide with the review whose
#: findings it posts: both are queue rows, and the queue is keyed by one
#: string. Built and read in this module only, which is what keeps the
#: format a format rather than an interface.
_PUBLISH_PREFIX = "publish:"


def publication_of(trigger: Trigger, run: RecordedRun) -> Trigger:
    """The work item that posts ``run``, to be enqueued when a post fails.

    Derived from the review's own trigger, so the item carries the actor and
    the comment the review was asked for by -- the queue row needs both, and
    a republished review is still that contributor's review.
    """
    return replace(
        trigger,
        kind=TriggerKind.PUBLISH,
        dedupe_key=_PUBLISH_PREFIX + run.dedupe_key,
        head_sha=run.head_sha,
    )


def published_run_key(trigger: Trigger) -> str:
    """Which recorded run a ``PUBLISH`` item names."""
    if trigger.kind is not TriggerKind.PUBLISH:
        raise ValueError(f"{trigger.dedupe_key!r} is not a publication item")
    return trigger.dedupe_key[len(_PUBLISH_PREFIX) :]


@dataclass(frozen=True)
class PullRequestHistory:
    """What earlier rounds on one pull request left behind.

    ``prior`` is the last *completed* round's findings, which is what the
    reviewer is shown so it can say "still" truthfully. ``high_water`` is the
    largest number ever issued here, including on findings that have since
    been fixed -- a retired number must never come back on something else.

    ``head_sha`` and ``recorded_at`` are that same last completed round's
    head and when it was recorded, ``None`` before the first. The head is
    what an incremental round diffs from; ``prior`` is deliberately *not*
    narrowed with it, because a finding on a line the narrower diff no
    longer shows is still one the reviewer has to say "still" about.
    """

    prior: tuple[Finding, ...]
    high_water: int
    head_sha: str | None = None
    recorded_at: datetime | None = None

    def incremental_base(self, now: datetime, min_seconds: int) -> str | None:
        """The head to diff from, or ``None`` when this round must be full.

        ``None`` before the first completed round, and when that round was
        recorded fewer than ``min_seconds`` ago -- ``budget.
        incremental_min_seconds``, where zero disables the threshold.
        """
        if self.recorded_at is None or (
            min_seconds and now - self.recorded_at < timedelta(seconds=min_seconds)
        ):
            return None
        return self.head_sha


class RunStore:
    """Durable record of what each paid review produced."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def record(
        self,
        trigger: Trigger,
        *,
        head_sha: str,
        result: ReviewResult,
        now: datetime,
        omitted: tuple[tuple[str, int], ...] = (),
    ) -> RecordedRun:
        """Store what this run produced, before anything is posted.

        ``head_sha`` is passed rather than read off the trigger: a mention's
        trigger carries no sha, and the one that matters is the head the
        review actually ran against, which the worker resolved when it
        claimed.

        Returns what was stored, so the caller publishes the row it just
        wrote rather than querying for "the oldest unpublished run" and
        hoping that is the same one.
        """
        with self._store.transaction() as conn:
            conn.execute(
                _RECORD,
                {
                    "key": trigger.dedupe_key,
                    "repo": trigger.repo,
                    "pr": trigger.pr_number,
                    "sha": head_sha,
                    "outcome": str(result.outcome),
                    "findings": _dump(result.findings),
                    "omitted": json.dumps(omitted),
                    "now": stamp(now, "run timestamp"),
                },
            )
        return RecordedRun(
            dedupe_key=trigger.dedupe_key,
            repo=trigger.repo,
            pr_number=trigger.pr_number,
            head_sha=head_sha,
            outcome=result.outcome,
            findings=result.findings,
            comment_id=None,
            omitted=omitted,
        )

    def unpublished(self, dedupe_key: str) -> RecordedRun | None:
        """The run ``dedupe_key`` names, or ``None`` if it needs no posting.

        ``None`` covers three cases a publication item does not distinguish:
        the run was posted by an earlier attempt, its content was purged
        after the pull request merged, or it was never recorded at all. All
        three mean the same thing to the caller -- there is nothing left to
        post -- and the item is finished either way.
        """
        with self._store.transaction() as conn:
            row = conn.execute(_UNPUBLISHED, {"key": dedupe_key}).fetchone()
        return None if row is None else _run(row)

    def history(self, repo: str, pr_number: int) -> PullRequestHistory:
        """What earlier completed rounds on this pull request produced."""
        with self._store.transaction() as conn:
            rows = conn.execute(_HISTORY, {"repo": repo, "pr": pr_number}).fetchall()
        rounds = [_load(row[0]) for row in rows]
        numbers = [f.number for round_ in rounds for f in round_ if f.number]
        history = PullRequestHistory(prior=(), high_water=max(numbers, default=0))
        if not rows:
            return history
        return replace(
            history, prior=rounds[0], head_sha=rows[0][1], recorded_at=parse(rows[0][2])
        )

    def round_of(self, repo: str, pr_number: int, dedupe_key: str) -> int:
        """Which round this run is, counting only reviews that produced one.

        A truncated or failed run posted nothing, so calling it a round would
        make the number a reader sees disagree with the comments they can
        actually find. An unrecorded key reads as round 1 rather than
        raising: this decides a header, and no header is worth failing a
        publish over.
        """
        with self._store.transaction() as conn:
            keys = [
                row[0] for row in conn.execute(_ROUNDS, {"repo": repo, "pr": pr_number})
            ]
        return keys.index(dedupe_key) + 1 if dedupe_key in keys else 1

    def mark_published(
        self,
        dedupe_key: str,
        *,
        comment_id: int | None,
        now: datetime,
        outcome: str,
    ) -> bool:
        """Record that this run needs publishing no longer.

        ``comment_id`` is ``None`` for a dry run, which posted nothing but
        still ran the pipeline: an unstamped run would be re-offered on
        every claim for the lifetime of the database.

        ``outcome`` says *how* it ended, which ``published_at`` cannot: a
        posted review and one discarded because the head moved both need
        stamping, and only the column tells them apart afterwards.

        ``False`` when the run was already stamped.
        """
        with self._store.transaction() as conn:
            return (
                conn.execute(
                    _MARK_PUBLISHED,
                    {
                        "key": dedupe_key,
                        "comment": comment_id,
                        "now": stamp(now, "run timestamp"),
                        "outcome": outcome,
                    },
                ).rowcount
                == 1
            )

    def publish_failed(self, dedupe_key: str, *, limit: int, now: datetime) -> bool:
        """Count a failed post; ``True`` when that was the last one allowed.

        The count lives on the run rather than on the queue row that carries
        it. A publication item is released *unattempted* -- posting reaches
        no engine and drains no allowance, so ``max_attempts`` has nothing
        to measure -- and the row is closed once this returns ``True``, so a
        counter held there would be decremented away and then thrown out.
        The stamp has to outlive both, because it is what an operator reads
        to find the review nobody ever saw, and clears to offer it again.
        """
        with self._store.transaction() as conn:
            conn.execute(
                _PUBLISH_FAILED,
                {"key": dedupe_key, "limit": limit, "now": stamp(now, "run timestamp")},
            )
            row = conn.execute(_PUBLISH_FAILED_AT, {"key": dedupe_key}).fetchone()
        return row is not None and row[0] is not None

    def purge_content(self, repo: str, pr_number: int, *, now: datetime) -> int:
        """Delete the review content for this pull request; how many rows.

        No caller yet -- the retention sweep is the next component, and it is
        specified in terms of this table. The shape of the purge is decided
        here because deciding it later would mean deciding it against rows
        already written the wrong way.
        """
        with self._store.transaction() as conn:
            return conn.execute(
                _PURGE,
                {"repo": repo, "pr": pr_number, "now": stamp(now, "run timestamp")},
            ).rowcount


def _dump(findings: tuple[Finding, ...]) -> str:
    """Findings as stored JSON."""
    return json.dumps(
        [
            {
                "path": f.path,
                "line": f.line,
                "severity": str(f.severity),
                "title": f.title,
                "body": f.body,
                "number": f.number,
            }
            for f in findings
        ]
    )


def _load(raw: str) -> tuple[Finding, ...]:
    """Findings as read back.

    ``title`` and ``number`` are read defensively because rows written before
    they existed are still in live databases, and this column carries no
    schema version of its own. An absent title reads as empty rather than
    raising: a review that was published once should not become unreadable.
    """
    return tuple(
        Finding(
            path=item["path"],
            line=item["line"],
            severity=Severity(item["severity"]),
            title=item.get("title", ""),
            body=item["body"],
            number=item.get("number"),
        )
        for item in json.loads(raw)
    )


def _run(row: tuple) -> RecordedRun:
    """Build a :class:`RecordedRun` from an ``_UNPUBLISHED`` row."""
    return RecordedRun(
        dedupe_key=row[0],
        repo=row[1],
        pr_number=row[2],
        head_sha=row[3],
        outcome=Outcome(row[4]),
        findings=_load(row[5]),
        comment_id=row[6],
        omitted=tuple((path, files) for path, files in json.loads(row[7])),
    )
