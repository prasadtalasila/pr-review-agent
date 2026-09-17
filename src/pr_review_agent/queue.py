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
budget reservation is specified to join that same transaction.

``Claim.trigger.head_sha`` is the head observed when the trigger was
*classified*, and for a mention it may be ``None`` because the comment payload
does not carry one. It is what the review runs against; the publisher re-reads
the live head immediately before posting, so a review of a superseded commit
is discarded rather than published late.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from ._compat import StrEnum
from .store import SqliteStore, to_utc
from .triggers.models import Trigger, TriggerKind

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
    (dedupe_key, kind, repo, pr_number, head_sha, actor_id, status, enqueued_at)
VALUES (:key, :kind, :repo, :pr, :sha, :actor, :pending, :now)
"""

# The complement of _CLAIMABLE's attempts test: a row that would otherwise be
# offered, but has no attempts left. Covers a lapsed lease and a row handed
# back by ``release``, so an exhausted trigger never lingers as "pending".
_ABANDON_EXHAUSTED = """
UPDATE queue SET status = :abandoned, leased_until = NULL, owner = NULL
WHERE attempts >= :max_attempts
  AND (status = :pending OR (status = :claimed AND leased_until <= :now))
"""

# The oldest row that is waiting (or whose lease has lapsed), has attempts
# left, and whose pull request nobody else is holding.
_CLAIMABLE = """
SELECT dedupe_key, kind, repo, pr_number, head_sha, actor_id, attempts
FROM queue AS q
WHERE q.attempts < :max_attempts
  AND (q.status = :pending OR (q.status = :claimed AND q.leased_until <= :now))
  AND NOT EXISTS (
      SELECT 1 FROM queue AS other
      WHERE other.repo = q.repo
        AND other.pr_number = q.pr_number
        AND other.status = :claimed
        AND other.leased_until > :now)
ORDER BY q.enqueued_at, q.rowid
LIMIT 1
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


class ReviewQueue:
    """Durable queue of accepted triggers, with one lease per pull request."""

    def __init__(
        self,
        store: SqliteStore,
        *,
        lease: timedelta = DEFAULT_LEASE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._store = store
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
            "pending": str(QueueStatus.PENDING),
            "now": _stamp(now, "enqueued_at"),
        }
        with self._store.transaction() as conn:
            return conn.execute(_ENQUEUE, params).rowcount == 1

    def claim(self, *, now: datetime, owner: str) -> Claim | None:
        """Lease the oldest claimable trigger, or ``None`` if there is none."""
        until = to_utc(now, "now") + self._lease
        common = {
            "now": _stamp(now, "now"),
            "max_attempts": self._max_attempts,
            "pending": str(QueueStatus.PENDING),
            "claimed": str(QueueStatus.CLAIMED),
        }
        with self._store.transaction() as conn:
            conn.execute(
                _ABANDON_EXHAUSTED,
                {**common, "abandoned": str(QueueStatus.ABANDONED)},
            )
            row = conn.execute(_CLAIMABLE, common).fetchone()
            if row is None:
                return None
            _take_lease(conn, key=row[0], owner=owner, until=until)
        return _claim(row, owner=owner, leased_until=until)

    def complete(self, claim: Claim) -> bool:
        """Mark ``claim`` reviewed; ``False`` if its lease is no longer held."""
        return self._finish(claim, QueueStatus.DONE)

    def release(self, claim: Claim) -> bool:
        """Hand ``claim`` back for another attempt, without waiting out its
        lease; ``False`` if the lease is no longer held."""
        return self._finish(claim, QueueStatus.PENDING)

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
        ),
        attempts=row[6] + 1,
        owner=owner,
        leased_until=leased_until,
    )


def _stamp(value: datetime, what: str) -> str:
    """Format a timestamp for storage, and for comparison *inside* SQL.

    Every value goes through ``to_utc`` first, so each one carries the same
    ``+00:00`` suffix and the same widths down to the second. The fractional
    part is the one variable-width field, and it is harmless: ``+`` sorts
    before ``.``, so a whole second still precedes the same second with a
    fraction. ISO-8601 therefore sorts lexicographically in the order the
    instants occur, which is what lets the lease-expiry predicates be plain
    SQL comparisons rather than a read-and-compare in Python.
    """
    return to_utc(value, what).isoformat()
