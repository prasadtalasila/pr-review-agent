"""How often one pull request may be reviewed, below the rolling windows.

The windows in :mod:`pr_review_agent.budget` bound a *total*: tokens in a
trailing duration, across everything the agent does. They cannot tell one
pull request consuming the day from thirty sharing it, and the difference
matters because a branch under active development produces triggers at the
rate somebody pushes to it -- each one paid for in full, and each one liable
to be superseded by the next push before it can be posted.

So this is a second question, asked of the same ledger and answered before a
reservation is written: **has this pull request been reviewed too recently,
or too often today?**

**A refusal here defers rather than drops.** It is an ordinary admission
refusal, so the queue row stays ``pending``, costs no attempt, and is offered
again on the next claim -- by which time the interval has usually passed and
the review runs against whatever the head is *then*. That is the coalescing
property the pacer is really for: a burst of pushes and mentions inside one
interval produces one review of the final head rather than one review each.

**The ledger, not the runs table.** ``runs`` holds reviews that produced
findings; a run that failed before recording anything spent tokens just the
same, and a pacer blind to it would let a crash-looping pull request through
every time. Every admitted run writes a ledger row, so the ledger is the
honest record of "this pull request reached an engine".

Rows written before the ledger carried a repository and a pull request read
as no history at all, which is the same answer a pull request nobody has
reviewed gets. A pacer that treated a missing column as *recent* would
refuse every review on an upgraded database until the backfill it cannot do
had happened.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from ._time import parse, stamp
from .config import BudgetConfig
from .config.budget import DAILY
from .queue import Claim
from .triggers.models import TriggerKind

logger = logging.getLogger(__name__)

_LAST_RESERVED_AT = """
SELECT MAX(reserved_at) FROM ledger WHERE repo = :repo AND pr_number = :pr
"""

_RESERVED_SINCE = """
SELECT COUNT(*) FROM ledger
WHERE repo = :repo AND pr_number = :pr AND reserved_at > :start
"""


def paced(
    conn: sqlite3.Connection, claim: Claim, now: datetime, config: BudgetConfig
) -> bool:
    """Whether this pull request may be reviewed *now*.

    ``False`` defers the claim: see the module docstring. Runs inside the
    claim's own transaction, like every other admission check, and writes
    nothing -- a deferral has no state to keep, because the ledger already
    says everything it needs to know.
    """
    last = _last_review(conn, claim)
    if last is None:
        return True
    return _within_interval(claim, now, last, config) and _under_cap(
        conn, claim, now, config
    )


def _last_review(conn: sqlite3.Connection, claim: Claim) -> datetime | None:
    """When this pull request last reached an engine, if it ever has."""
    row = conn.execute(
        _LAST_RESERVED_AT,
        {"repo": claim.trigger.repo, "pr": claim.trigger.pr_number},
    ).fetchone()
    return parse(row[0]) if row and row[0] else None


def _within_interval(
    claim: Claim, now: datetime, last: datetime, config: BudgetConfig
) -> bool:
    """Whether the interval since the last review has elapsed."""
    interval = config.review_interval(mention=claim.trigger.kind is TriggerKind.MENTION)
    if not interval or now - last >= interval:
        return True
    logger.warning(
        "%s#%d was reviewed at %s: deferring %s until the %ds interval elapses",
        claim.trigger.repo,
        claim.trigger.pr_number,
        stamp(last),
        claim.trigger.dedupe_key,
        int(interval.total_seconds()),
        extra={"repo": claim.trigger.repo, "pr": claim.trigger.pr_number},
    )
    return False


def _under_cap(
    conn: sqlite3.Connection, claim: Claim, now: datetime, config: BudgetConfig
) -> bool:
    """Whether this pull request has reviews left in its trailing day.

    ``DAILY`` is the governor's own window duration, imported rather than
    restated: the cap answers the question the daily window answers -- what
    may be spent in a day -- scoped to one pull request, and two copies of
    "a day" are two things that can disagree.
    """
    cap = config.max_reviews_per_pull_request
    if cap is None:
        return True
    row = conn.execute(
        _RESERVED_SINCE,
        {
            "repo": claim.trigger.repo,
            "pr": claim.trigger.pr_number,
            "start": stamp(now - DAILY),
        },
    ).fetchone()
    reviews = int(row[0]) if row else 0
    if reviews < cap:
        return True
    logger.warning(
        "%s#%d has had %d reviews in the last day (cap %d): refusing %s",
        claim.trigger.repo,
        claim.trigger.pr_number,
        reviews,
        cap,
        claim.trigger.dedupe_key,
        extra={"repo": claim.trigger.repo, "pr": claim.trigger.pr_number},
    )
    return False
