"""The SQLite state that has to survive a restart.

Four things are kept here, and the first three are about *not repeating
work*:

**Watermarks.** The poller sees open pull requests and recent comments, not
``opened`` events, so the classifier needs a timestamp below which everything
has already been considered. Held only in memory, a restart re-offers the
whole open backlog -- and, for comments, replays every old ``@claude`` as a
fresh request. That is not a wasted poll; it spends the weekly allowance.

**ETags.** Cheaper to lose: a missing ETag costs one full GET per endpoint.
It lives here because it is the same table shape and the same lifetime.

**The queue.** Accepted triggers waiting for a worker, and the per-pull-request
leases that stop two workers reviewing one pull request at once. The table is
declared here because this module owns the schema; the claim protocol that
operates on it lives in :mod:`pr_review_agent.queue`.

**The ledger.** Every reservation the budget governor takes and every run it
settles. Unlike the other three it is a *record*, not a cache: the rolling
windows are computed from it, so its rows are append-only and are never
pruned -- not even when review content is purged after a merge. Deleting one
would silently hand back allowance that was genuinely spent. The arithmetic
over it lives in :mod:`pr_review_agent.budget`.

A watermark only ever moves forward. A restart that read a stale row, or two
cycles settling out of order, must not walk it backwards and re-admit events
that were already decided -- so :meth:`SqliteStore.advance_watermark` takes
the later of the stored and the offered value.

WAL mode is on because the daemon's later phases (queue, lease, budget
governor) read this file while the poller writes it. The write lock stays
single-writer either way, which is the property the budget reservation
depends on.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ._time import parse, stamp, to_utc

# Applied in order; the file's ``user_version`` records how many have run.
# One migration is a tuple of whole statements, not a script: a statement is
# never split up at runtime, so a ``CHECK (x IN ('a;b'))``, a trigger body or
# a comment carrying a semicolon cannot half-apply a migration.
#
# The ``CREATE`` statements are all ``IF NOT EXISTS`` because a database
# created before this list existed already carries the first migration's
# tables at ``user_version = 0``, and would otherwise fail to adopt it.
#
# They no longer have to be idempotent for crash-safety: ``_migrate`` applies
# each migration and its version bump in one transaction, so a crash rolls
# the pair back together. ``ALTER TABLE ADD COLUMN`` has no ``IF NOT EXISTS``
# form in SQLite and could not have been written any other way.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE IF NOT EXISTS etags (
            path TEXT PRIMARY KEY,
            etag TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS watermarks (
            name TEXT PRIMARY KEY,
            at   TEXT NOT NULL
        )
        """,
    ),
    (
        """
        CREATE TABLE IF NOT EXISTS queue (
            dedupe_key   TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            repo         TEXT NOT NULL,
            pr_number    INTEGER NOT NULL,
            head_sha     TEXT,
            actor_id     INTEGER NOT NULL,
            status       TEXT NOT NULL,
            attempts     INTEGER NOT NULL DEFAULT 0,
            enqueued_at  TEXT NOT NULL,
            leased_until TEXT,
            owner        TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS queue_claimable ON queue (status, enqueued_at)",
        "CREATE INDEX IF NOT EXISTS queue_by_pr ON queue (repo, pr_number, status)",
    ),
    (
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key       TEXT NOT NULL,
            owner            TEXT NOT NULL,
            actor_id         INTEGER NOT NULL,
            mode             TEXT NOT NULL,
            reserved_tokens  INTEGER NOT NULL,
            used_tokens      INTEGER,
            usage_confidence TEXT,
            engine           TEXT,
            model            TEXT,
            reserved_at      TEXT NOT NULL,
            settled_at       TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS ledger_window ON ledger (reserved_at)",
    ),
    # The per-contributor window measures one ``actor_id`` over a trailing
    # duration, which is the first query to select on anything but time.
    ("CREATE INDEX IF NOT EXISTS ledger_by_actor ON ledger (actor_id, reserved_at)",),
    ("ALTER TABLE ledger ADD COLUMN reviewed_lines INTEGER",),
    # Why a run stopped, which is a different question from how far its
    # recorded cost can be trusted. A run killed on the wall clock and one
    # whose envelope would not parse both settle `unavailable` at the full
    # reservation, so without this column nothing says which control bound
    # the run.
    ("ALTER TABLE ledger ADD COLUMN stop_reason TEXT",),
    # What the publisher acknowledges on. Both are NULL for a `pr_opened`
    # row, and for any row enqueued before this migration -- the publisher
    # falls back to reacting on the pull request rather than guessing an id.
    (
        "ALTER TABLE queue ADD COLUMN comment_id INTEGER",
        "ALTER TABLE queue ADD COLUMN comment_source TEXT",
    ),
    # What a paid review produced. The only table holding review content,
    # and therefore the only one the retention sweep purges; the ledger's
    # metrics survive that purge because they live elsewhere. See runs.py.
    (
        """
        CREATE TABLE IF NOT EXISTS runs (
            dedupe_key        TEXT PRIMARY KEY,
            repo              TEXT NOT NULL,
            pr_number         INTEGER NOT NULL,
            head_sha          TEXT NOT NULL,
            outcome           TEXT NOT NULL,
            findings          TEXT NOT NULL,
            comment_id        INTEGER,
            recorded_at       TEXT NOT NULL,
            published_at      TEXT,
            content_purged_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS runs_by_pr ON runs (repo, pr_number)",
    ),
    # The circuit breaker's three scalars. A separate table from `ledger`
    # because ledger rows are tokens genuinely consumed and the rolling
    # windows sum them; breaker state is not usage and must not be summed.
    (
        """
        CREATE TABLE IF NOT EXISTS budget_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ),
    # The shared budget policy: whose configuration governs the pool when
    # several daemons share this file. One row, held there by the CHECK,
    # because a second row would be a second opinion about one allowance --
    # and two daemons that disagree do not split the pool between them, they
    # hand it to whichever was configured most permissively.
    #
    # Separate from `budget_state` even though both are one-row-ish scalars:
    # that one is what the breaker *learned*, this is what an operator
    # *declared*, and a reset of either must not touch the other.
    (
        """
        CREATE TABLE IF NOT EXISTS budget_policy (
            id             INTEGER PRIMARY KEY CHECK (id = 1),
            authority_repo TEXT NOT NULL,
            policy         TEXT NOT NULL,
            written_at     TEXT NOT NULL
        )
        """,
    ),
    # Which pull request a reservation was made for. The ledger is the
    # record of what reached an engine, so it is the only place that can
    # answer "how recently, and how often, has this pull request been
    # reviewed" about *every* run rather than only the ones that produced
    # findings -- a failed run spent tokens too. Both columns are NULL on
    # every row written before this migration, which the pacer reads as "no
    # history", the same answer it gives a pull request nobody has reviewed.
    (
        "ALTER TABLE ledger ADD COLUMN repo TEXT",
        "ALTER TABLE ledger ADD COLUMN pr_number INTEGER",
        "CREATE INDEX IF NOT EXISTS ledger_by_pr "
        "ON ledger (repo, pr_number, reserved_at)",
    ),
    # How publishing a run ended, rather than only whether it is still owed
    # a comment. `published_at` answers "does this still need posting"; it
    # cannot distinguish a review that was posted from one discarded because
    # the head moved, and both have to be stamped or the row is offered for
    # publication for the lifetime of the database.
    ("ALTER TABLE runs ADD COLUMN publish_outcome TEXT",),
    # How many times posting this run has been tried, and when it was given
    # up on. A publication item is retried without counting an attempt --
    # posting reaches no engine, so the bound that measures allowance has
    # nothing to measure -- which left a post GitHub will never accept being
    # retried on every claim forever. Rows written before this migration
    # start at zero, which is the honest answer: nothing counted them.
    (
        "ALTER TABLE runs ADD COLUMN publish_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE runs ADD COLUMN publish_failed_at TEXT",
    ),
    # Which comments the agent itself posted. A table of its own rather than
    # a column on `runs`: `runs` holds reviews, and the set has to cover
    # every comment the agent posts whether or not a review is behind it --
    # otherwise the first comment posted from some other path is a comment
    # the classifier will accept as somebody else's. Kept forever, like the
    # ledger, so a retention sweep over `runs` cannot erase what the agent
    # said.
    (
        """
        CREATE TABLE IF NOT EXISTS agent_comments (
            repo       TEXT NOT NULL,
            comment_id INTEGER NOT NULL,
            posted_at  TEXT NOT NULL,
            PRIMARY KEY (repo, comment_id)
        )
        """,
    ),
    # What identifies a reservation while it is open: the trigger it was
    # taken for, and nothing else. `settle` matches on the key and the owner,
    # so a worker that crashed holding a reservation and then re-claimed the
    # same row under the same owner left two open rows for one settle to
    # match -- which updated both, reported no single reservation, and made
    # the worker throw away a review it had already paid for. The rule was
    # always one open reservation per key; only the schema can hold it.
    #
    # The cleanup runs first because the index cannot be created over a
    # database that already holds duplicates, and a daemon whose store will
    # not migrate does not start. Live databases exist, so that is not a
    # theoretical case. The newest open row per key survives -- it is the one
    # a running worker may still settle -- and the older ones close at their
    # full `reserved_tokens`: what a worker that never settled actually spent
    # is unknowable, and for a spending control the pessimistic reading is
    # the safe one. `settled_at` is the row's own `reserved_at` rather than
    # the migration's clock, because nothing was learned about that run after
    # the instant it opened, and a later timestamp would suggest otherwise.
    #
    # Both statements are one migration, so the cleanup and the constraint
    # commit together: a crash between them cannot leave a database with
    # duplicates and no index, or an index nothing enforced.
    (
        """
        UPDATE ledger
        SET used_tokens = reserved_tokens, usage_confidence = 'unavailable',
            stop_reason = 'lost', settled_at = reserved_at
        WHERE settled_at IS NULL AND id NOT IN (
            SELECT MAX(id) FROM ledger WHERE settled_at IS NULL
            GROUP BY dedupe_key
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS ledger_open "
        "ON ledger (dedupe_key) WHERE settled_at IS NULL",
    ),
    # The head an incremental round diffed from, NULL for a full round. The
    # pre-flight fit reads only full rounds: an incremental one carries the
    # same fixed prompt overhead over fewer lines, and fitting it would pull
    # the rate below what a full review costs -- the direction that
    # under-refuses. Rows written before this are all full rounds, which is
    # what NULL says.
    ("ALTER TABLE ledger ADD COLUMN reviewed_since TEXT",),
    # What `budget.excluded_paths` withheld from a run's diff, as the JSON
    # list of `[path, files]` pairs the coverage footer renders. On the run
    # rather than recomputed at publish time, because a retried post happens
    # after the checkout is gone. Rows written before this read as empty:
    # nothing recorded what they left out, and the footer says nothing.
    ("ALTER TABLE runs ADD COLUMN omitted TEXT NOT NULL DEFAULT '[]'",),
    # The reviewer's assessment of the whole pull request (issue #126), as a
    # JSON object, rendered on a line under the header. NULL for rows written
    # before it existed, and for purged ones: `priority_files` are paths
    # from the contributor's tree. A NULL renders no line.
    ("ALTER TABLE runs ADD COLUMN assessment TEXT",),
)

SCHEMA_VERSION = len(_MIGRATIONS)


def is_contention(exc: sqlite3.OperationalError) -> bool:
    """Whether ``exc`` is another connection holding the write lock.

    ``busy_timeout`` above waits 5 s for that lock, and several daemons
    sharing one store can exceed it -- the reviews are minutes long and the
    writes are not staggered. SQLite reports the timeout as a plain
    ``OperationalError``; the message is the only thing distinguishing it
    from a genuine fault like a missing table, so the message is what this
    reads. ``SQLITE_BUSY`` says "database is locked" and ``SQLITE_LOCKED``
    says "database table is locked", so the shared word is the test. No
    other message the driver raises contains either word, and matching too
    narrowly only costs the restart the caller would have done anyway.
    """
    message = str(exc).lower()
    return "locked" in message or "busy" in message


class SqliteStore:
    """Persistent ETags, watermarks and review queue for one daemon instance."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # A reader that meets the single write lock should wait for it rather
        # than raise "database is locked" on the spot.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- Schema -----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """How many migrations this database has had applied."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        """Apply every migration this database has not seen yet.

        Each migration and its version bump commit together. ``executescript``
        would be the natural way to run several statements, but it issues a
        ``COMMIT`` of its own first, which would split the pair -- so the
        statements are executed individually inside one transaction instead.
        A crash mid-migration therefore rolls back to the previous version
        and the migration is simply re-applied, rather than needing every
        statement to be independently idempotent.
        """
        for index, statements in enumerate(
            _MIGRATIONS[self.schema_version :], start=self.schema_version + 1
        ):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    self._conn.execute(statement)
                # PRAGMA does not accept a bound parameter; `index` is a loop
                # counter over a module constant, never user input.
                self._conn.execute(f"PRAGMA user_version = {index:d}")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one ``BEGIN IMMEDIATE`` write transaction.

        The write lock is taken up front rather than on first write, which is
        what makes a read-then-write sequence -- such as the queue's
        conditional claim -- atomic against another writer. The budget
        reservation is specified to join this same transaction, so that a
        claim and the allowance it spends commit together or not at all.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- ETag cache (satisfies poller.etag_store.ETagCache) ---------------

    def get(self, path: str) -> str | None:
        """The last ETag seen for ``path``, or ``None`` on a cold start."""
        row = self._conn.execute(
            "SELECT etag FROM etags WHERE path = ?", (path,)
        ).fetchone()
        return None if row is None else row[0]

    def set(self, path: str, etag: str | None) -> None:
        """Record ``etag`` for ``path``; a ``None`` etag forgets the entry."""
        if etag is None:
            self._conn.execute("DELETE FROM etags WHERE path = ?", (path,))
        else:
            self._conn.execute(
                "INSERT INTO etags (path, etag) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET etag = excluded.etag",
                (path, etag),
            )

    # -- Watermarks -------------------------------------------------------

    def watermark(self, name: str) -> datetime | None:
        """The stored watermark for ``name``, as an aware UTC datetime."""
        row = self._conn.execute(
            "SELECT at FROM watermarks WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else parse(row[0])

    def adopt_legacy_watermarks(
        self, qualified: Mapping[str, str]
    ) -> dict[str, datetime]:
        """Rename pre-multi-repo watermark rows, once per store.

        ``qualified`` maps each bare stem to the name it should take. A
        database written before watermarks carried a repository holds bare
        ``pull_requests`` and ``comments`` rows, and no schema migration can
        rename them: the repository is named in ``config.yaml`` and is not in
        the database at all.

        **Once per store, not once per repository.** The rows are the first
        repository's, so a second repository pointed at the same file must
        not inherit them -- its watermark would start weeks in the past and
        its whole open backlog would be enqueued and paid for. Adoption
        therefore happens only while no qualified row exists anywhere, and
        the legacy rows are deleted in the same transaction so that a later
        arrival cannot adopt them. That trades a downgrade's watermark for a
        bound on spending, which is the direction this project errs in.

        Returns the names adopted, for the caller to log.
        """
        adopted: dict[str, datetime] = {}
        with self.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM watermarks WHERE name LIKE '%:%' LIMIT 1"
                ).fetchone()
                is not None
            ):
                return adopted
            for stem, name in qualified.items():
                row = conn.execute(
                    "SELECT at FROM watermarks WHERE name = ?", (stem,)
                ).fetchone()
                if row is None:
                    continue
                conn.execute(
                    "INSERT INTO watermarks (name, at) VALUES (?, ?)", (name, row[0])
                )
                adopted[name] = parse(row[0])
            conn.execute("DELETE FROM watermarks WHERE name NOT LIKE '%:%'")
        return adopted

    def advance_watermark(self, name: str, at: datetime) -> datetime:
        """Move the ``name`` watermark forward to ``at``, never backwards.

        Returns the watermark in force afterwards, which is the later of the
        stored and the offered value.
        """
        at = to_utc(at, "watermark")
        current = self.watermark(name)
        if current is not None and current >= at:
            return current
        self._conn.execute(
            "INSERT INTO watermarks (name, at) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET at = excluded.at",
            (name, at.isoformat()),
        )
        return at

    # -- Shared budget policy ---------------------------------------------

    def budget_policy(self) -> BudgetPolicy | None:
        """The published policy, or ``None`` if no authority has run yet."""
        return read_budget_policy(self._conn)

    def publish_budget_policy(self, policy: BudgetPolicy, *, now: datetime) -> None:
        """Replace the published policy with ``policy``.

        Unconditional, so an authority restarting or reloading republishes
        rather than having to reconcile: its file is the declared truth, and
        the row is only ever a copy of it.
        """
        self._conn.execute(
            "INSERT INTO budget_policy (id, authority_repo, policy, written_at) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET authority_repo = excluded.authority_repo, "
            "policy = excluded.policy, written_at = excluded.written_at",
            (
                policy.authority_repo,
                json.dumps(policy.fields, sort_keys=True),
                stamp(now, "written_at"),
            ),
        )


@dataclass(frozen=True)
class BudgetPolicy:
    """The pool arithmetic one daemon published for the others to adopt."""

    authority_repo: str
    fields: dict[str, int | None]


def read_budget_policy(conn: sqlite3.Connection) -> BudgetPolicy | None:
    """The published policy, read on a caller's connection.

    Takes a connection rather than a store so the governor can read it inside
    the transaction its reservation is already being written in, which is what
    lets an authority's change reach a running complier with no signal to it.
    """
    row = conn.execute(
        "SELECT authority_repo, policy FROM budget_policy WHERE id = 1"
    ).fetchone()
    return None if row is None else BudgetPolicy(row[0], json.loads(row[1]))
