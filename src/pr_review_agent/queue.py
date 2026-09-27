"""The review queue and its per-pull-request leases.

An accepted :class:`~pr_review_agent.triggers.models.Trigger` is not reviewed
where it is classified. It is enqueued, and a worker claims it later. That
indirection is what the spending rails need: a claim is the single point at
which work becomes expensive, so it is the single point the budget governor
has to guard.

Four rules shape the table, and each one exists to stop a review being paid
for twice.

**A trigger is enqueued at most once.** The dedupe key is the primary key, so
a re-poll that sees the same freshly opened pull request, or the same
``@claude`` comment, inserts nothing. Rows are kept after completion for
exactly this reason: the key is what makes "already reviewed" a fact rather
than a guess.

**One pull request is reviewed by one worker at a time.** A claim is refused
while any other row for the same ``(repo, pr_number)`` holds a live lease --
so a maintainer's ``@claude`` arriving mid-review waits for the run in
flight instead of racing it.

**A crashed worker releases its work, and only so often.** The lease carries
an expiry rather than a heartbeat: a run has a wall-clock ceiling, so a lease
set above that ceiling cannot expire under a worker that is still alive, and
renewal would be machinery for a case that cannot arise. An expired lease
makes the row claimable again, bounded by ``max_attempts`` -- without that
bound a trigger that crashes its worker every time would be re-reviewed
forever, and each attempt spends allowance before it fails.

**The claim is atomic.** SQLite has no ``SKIP LOCKED``, so the read that
picks a row and the write that leases it run inside one ``BEGIN IMMEDIATE``
transaction (:meth:`~pr_review_agent.store.SqliteStore.transaction`). The
budget reservation joins that same transaction through ``claim``'s ``admit``
hook, so a claim and the allowance it spends commit together or not at all.

``Claim.trigger.head_sha`` is the head observed when the trigger was
*classified*, and for a mention it may be ``None`` because the comment payload
does not carry one. It is what the review runs against; the publisher re-reads
the live head immediately before posting, so a review of a superseded commit
is discarded rather than published late.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ._compat import StrEnum
from ._time import stamp, to_utc
from .store import SqliteStore
from .triggers.models import CommentSource, Trigger, TriggerKind

#: The budget governor's hook into :meth:`ReviewQueue.claim`. It is handed
#: the claim's own transaction, so whatever it writes commits with the lease
#: or not at all. Returning ``False`` skips the candidate.
Admit = Callable[[sqlite3.Connection, "Claim", datetime], bool]

# Comfortably above the per-run wall-clock ceiling the budget governor
# enforces, so a live worker never loses its lease; see the module docstring.
DEFAULT_LEASE = timedelta(minutes=30)

# Three runs is enough to ride out a transient failure and few enough that a
# poison trigger cannot drain the weekly allowance one retry at a time.
DEFAULT_MAX_ATTEMPTS = 3


class QueueStatus(StrEnum):
    """Where a queued trigger has got to."""

    PENDING = "pending"
    CLAIMED = "claimed"
    DONE = "done"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class Claim:
    """One trigger, leased to ``owner`` until ``leased_until``.

    The trigger is carried whole rather than unpacked, so a worker holds
    exactly what the classifier accepted.
    """

    trigger: Trigger
    attempts: int
    owner: str
    leased_until: datetime


_ENQUEUE = """
INSERT OR IGNORE INTO queue
    (dedupe_key, kind, repo, pr_number, head_sha, actor_id, status, enqueued_at,
     comment_id, comment_source)
VALUES (:key, :kind, :repo, :pr, :sha, :actor, :pending, :now,
        :comment_id, :comment_source)
"""

# The complement of _CLAIMABLE's attempts test: a row that would otherwise be
# offered, but has no attempts left. Covers a lapsed lease and a row handed
# back by ``release``, so an exhausted trigger never lingers as "pending".
_ABANDON_EXHAUSTED = """
UPDATE queue SET status = :abandoned, leased_until = NULL, owner = NULL
WHERE repo = :repo
  AND attempts >= :max_attempts
  AND (status = :pending OR (status = :claimed AND leased_until <= :now))
"""

# Every row that is waiting (or whose lease has lapsed), has attempts left,
# and whose pull request nobody else is holding, oldest first.
#
# There is deliberately no ``LIMIT 1``. Every condition below is a fact about
# the row -- attempts, status, lease, pull request -- so SQLite can already
# exclude anything unclaimable, which is what made one row enough before
# anything could refuse. A budget refusal is the first decision SQLite cannot
# express: it depends on the ledger, the ladder rung and the trigger's kind,
# so it happens in Python, by which point a single row would have discarded
# every alternative. See ``claim``.
_CLAIMABLE = """
SELECT dedupe_key, kind, repo, pr_number, head_sha, actor_id, attempts,
       comment_id, comment_source
FROM queue AS q
WHERE q.repo = :repo
  AND q.attempts < :max_attempts
  AND (q.status = :pending OR (q.status = :claimed AND q.leased_until <= :now))
  AND NOT EXISTS (
      SELECT 1 FROM queue AS other
      WHERE other.repo = q.repo
        AND other.pr_number = q.pr_number
        AND other.status = :claimed
        AND other.leased_until > :now)
ORDER BY q.enqueued_at, q.rowid
"""

_TAKE_LEASE = """
UPDATE queue
SET status = :claimed, attempts = attempts + 1, leased_until = :until,
    owner = :owner
WHERE dedupe_key = :key
"""

# Guarded on the owner: a worker whose lease lapsed and was re-claimed by
# somebody else must not be able to finish the newer worker's row.
_FINISH = """
UPDATE queue SET status = :status, leased_until = NULL, owner = NULL
WHERE dedupe_key = :key AND owner = :owner
"""

# `release`, minus the attempt. The bound exists so a poison trigger cannot
# drain the weekly allowance one retry at a time; work that reached no engine
# drained nothing, so counting it would abandon a review that has already been
# paid for. MAX() because attempts is only ever decremented inside the claim
# that incremented it, and a floor is cheaper than trusting that forever.
_RELEASE_UNATTEMPTED = """
UPDATE queue
SET status = :status, leased_until = NULL, owner = NULL,
    attempts = MAX(attempts - 1, 0)
WHERE dedupe_key = :key AND owner = :owner
"""

# Every other trigger for this pull request that the review just posted has
# already answered. Two conditions, and each one narrows the claim:
#
# `enqueued_at <= :before` -- it was already waiting when the review started,
# so what the review read is at least as new as what it asked about. A
# trigger enqueued *during* the review may be about something newer.
#
# `head_sha IS NULL OR head_sha = :head` -- it did not name a different
# commit. A mention carries no sha (the comment payload has none), which is
# the case this is really for: three people asking about one pull request
# asked one question. A trigger that named a commit the review did not read
# is not answered by it and keeps its own claim.
#
# A publication item is excluded: it names a recorded run of its own, which
# no review of this one has posted.
_FOLD = """
UPDATE queue SET status = :done, leased_until = NULL, owner = NULL
WHERE repo = :repo AND pr_number = :pr AND status = :pending
  AND kind != :publish AND dedupe_key != :key AND enqueued_at <= :before
  AND (head_sha IS NULL OR head_sha = :head)
"""

# Whether this worker still holds the row it claimed. Read immediately before
# a GitHub write that cannot be taken back.
_HOLDS = """
SELECT 1 FROM queue
WHERE dedupe_key = :key AND owner = :owner
  AND status = :claimed AND leased_until > :now
"""


class ReviewQueue:
    """Durable queue of accepted triggers, with one lease per pull request.

    Scoped to ``repo``: several daemons, one per repository, share a store so
    they can share a budget, and the queue table is shared with it. A claim
    that crossed repositories would hand a trigger to the one process that
    does not hold a token for it -- each daemon holds only its own, which is
    the trust boundary the per-process split exists to enforce.
    """

    def __init__(
        self,
        store: SqliteStore,
        *,
        repo: str,
        lease: timedelta = DEFAULT_LEASE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._store = store
        self._repo = repo
        self._lease = lease
        self._max_attempts = max_attempts

    def enqueue(self, trigger: Trigger, *, now: datetime) -> bool:
        """Add ``trigger``; ``False`` if its dedupe key is already known."""
        params = {
            "key": trigger.dedupe_key,
            "kind": str(trigger.kind),
            "repo": trigger.repo,
            "pr": trigger.pr_number,
            "sha": trigger.head_sha,
            "actor": trigger.actor_id,
            "comment_id": trigger.comment_id,
            "comment_source": _text(trigger.comment_source),
            "pending": str(QueueStatus.PENDING),
            "now": stamp(now, "enqueued_at"),
        }
        with self._store.transaction() as conn:
            return conn.execute(_ENQUEUE, params).rowcount == 1

    def claim(
        self, *, now: datetime, owner: str, admit: Admit | None = None
    ) -> Claim | None:
        """Lease the oldest claimable trigger ``admit`` accepts.

        ``admit`` runs **inside** this transaction, which is what makes a
        budget reservation atomic with the claim: two workers cannot both
        observe the same allowance and both spend it. It is a plain
        predicate, so no budget type appears in this signature and
        :mod:`pr_review_agent.queue` imports nothing from the governor.

        A refused candidate is skipped rather than ending the claim. It keeps
        its ``pending`` status and its attempt count -- a refusal is about the
        allowance, not about the trigger, and burning an attempt would let
        three refusals abandon a perfectly good one. Without skipping, a
        refused pull request would sit at the head of a FIFO queue and block a
        maintainer's ``@claude`` behind it for as long as the window took to
        roll.
        """
        until = to_utc(now, "now") + self._lease
        common = {
            "now": stamp(now, "now"),
            "repo": self._repo,
            "max_attempts": self._max_attempts,
            "pending": str(QueueStatus.PENDING),
            "claimed": str(QueueStatus.CLAIMED),
        }
        with self._store.transaction() as conn:
            conn.execute(
                _ABANDON_EXHAUSTED,
                {**common, "abandoned": str(QueueStatus.ABANDONED)},
            )
            # Read the candidates out before leasing one: committing with a
            # half-consumed cursor still open raises "SQL statements in
            # progress", and the claimable set is bounded by the pending
            # queue, which is small.
            for row in conn.execute(_CLAIMABLE, common).fetchall():
                candidate = _claim(row, owner=owner, leased_until=until)
                if admit is not None and not admit(conn, candidate, now):
                    continue
                _take_lease(conn, key=row[0], owner=owner, until=until)
                return candidate
            return None

    def complete(self, claim: Claim) -> bool:
        """Mark ``claim`` reviewed; ``False`` if its lease is no longer held."""
        return self._finish(claim, QueueStatus.DONE)

    def release(self, claim: Claim) -> bool:
        """Hand ``claim`` back for another attempt, without waiting out its
        lease; ``False`` if the lease is no longer held."""
        return self._finish(claim, QueueStatus.PENDING)

    def release_unattempted(self, claim: Claim) -> bool:
        """Hand ``claim`` back **without** counting the attempt.

        For work that reached no review engine and so spent nothing: the
        publish-only retry. ``max_attempts`` bounds how much allowance one
        poison trigger may drain, and a run that drained none has no business
        being measured against it -- three failed *posts* of a review that
        was already paid for would otherwise abandon it, leaving the
        findings recorded and permanently invisible.
        """
        params = {
            "status": str(QueueStatus.PENDING),
            "key": claim.trigger.dedupe_key,
            "owner": claim.owner,
        }
        with self._store.transaction() as conn:
            return conn.execute(_RELEASE_UNATTEMPTED, params).rowcount == 1

    def fold(self, claim: Claim, *, before: datetime, head_sha: str) -> int:
        """Close the triggers ``claim``'s review already answered.

        Three maintainers mentioning the agent on one pull request asked one
        question, and the agent posts one comment per pull request -- so
        reviewing each of them separately would pay three times to overwrite
        the same comment twice. ``before`` is when this review started:
        everything still waiting at that moment is answered by it, and
        anything enqueued since is not. ``head_sha`` is the commit it read,
        which is what keeps a trigger naming some *other* commit out of the
        fold.

        Returns how many rows were folded, for the caller to log. Unguarded
        by the lease on purpose: the rows being closed are not the claimed
        one, and they are closed on the strength of a comment that has
        already been posted.
        """
        params = {
            "done": str(QueueStatus.DONE),
            "pending": str(QueueStatus.PENDING),
            "publish": str(TriggerKind.PUBLISH),
            "repo": self._repo,
            "pr": claim.trigger.pr_number,
            "key": claim.trigger.dedupe_key,
            "before": stamp(before, "fold boundary"),
            "head": head_sha,
        }
        with self._store.transaction() as conn:
            return conn.execute(_FOLD, params).rowcount

    def holds(self, claim: Claim, *, now: datetime) -> bool:
        """Does ``claim``'s owner still hold a live lease on its row?

        ``complete``, ``release`` and ``settle`` are all owner-guarded, which
        is enough when the thing being discarded is a database row. It is not
        enough when it is a comment: those guards run *after* the write. This
        is the same question asked before one.
        """
        params = {
            "key": claim.trigger.dedupe_key,
            "owner": claim.owner,
            "claimed": str(QueueStatus.CLAIMED),
            "now": stamp(now, "now"),
        }
        with self._store.transaction() as conn:
            return conn.execute(_HOLDS, params).fetchone() is not None

    def abandon(self, claim: Claim) -> bool:
        """Give ``claim`` up permanently; ``False`` if its lease is gone.

        For a failure that will fail again: an oversized pull request, an
        unusable payload. ``complete`` is not reused for it because ``done``
        means *reviewed*, and an operator reading the table should not have
        to guess which kind of ``done`` they are looking at. Retrying instead
        would reach the same refusal twice more, reserving allowance each
        time.
        """
        return self._finish(claim, QueueStatus.ABANDONED)

    def status(self, dedupe_key: str) -> QueueStatus | None:
        """The status of ``dedupe_key``, or ``None`` if it was never queued."""
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM queue WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
        return None if row is None else QueueStatus(row[0])

    def _finish(self, claim: Claim, status: QueueStatus) -> bool:
        params = {
            "status": str(status),
            "key": claim.trigger.dedupe_key,
            "owner": claim.owner,
        }
        with self._store.transaction() as conn:
            return conn.execute(_FINISH, params).rowcount == 1


def _take_lease(
    conn: sqlite3.Connection, *, key: str, owner: str, until: datetime
) -> None:
    """Mark the chosen row claimed, and count the attempt against its bound."""
    conn.execute(
        _TAKE_LEASE,
        {
            "claimed": str(QueueStatus.CLAIMED),
            "until": until.isoformat(),
            "owner": owner,
            "key": key,
        },
    )


def _claim(row: tuple, *, owner: str, leased_until: datetime) -> Claim:
    """Build a :class:`Claim` from a ``_CLAIMABLE`` row."""
    return Claim(
        trigger=Trigger(
            kind=TriggerKind(row[1]),
            repo=row[2],
            pr_number=row[3],
            head_sha=row[4],
            actor_id=row[5],
            dedupe_key=row[0],
            comment_id=row[7],
            comment_source=None if row[8] is None else CommentSource(row[8]),
        ),
        attempts=row[6] + 1,
        owner=owner,
        leased_until=leased_until,
    )


def _text(source: CommentSource | None) -> str | None:
    """A comment source as it is stored, or ``None`` when there is none."""
    return None if source is None else str(source)
