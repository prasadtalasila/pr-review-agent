"""The comments the agent itself has posted, so it cannot answer them.

There is no check on which account the agent posts as: ``github.agent_user_id``
and the ``self_author``/``self_commenter`` rules were removed in issue #36,
because an identity check rejects an *account*, and a deployment sharing one
account between the reviewer and the reviewed could then trigger nothing at
all. This is the same question asked of the *comment* instead -- did I post
this? -- which no configuration can get wrong, because nobody writes it down.

It is a structural constraint rather than a mitigation. Until 1.3.0 the agent
rewrote one comment per pull request, so its id never changed and the queue's
dedupe key bounded a self-review loop to one extra paid review however badly
:func:`pr_review_agent.triggers.mention.neutralise` failed. Posting a comment
per review made every round mint a new id and a new key, and that bound went
with it (issue #108). This restores it, and tightens it: the agent's own
comment is now rejected before a trigger exists, so the loop costs nothing
rather than one review.

``neutralise`` stays in front of it. This set is database state -- a store
restored from scratch, a second instance watching the same repository, or a
crash between the ``POST`` and the row written here all leave an id it never
learned -- and the escaped text is what covers those.
"""

from __future__ import annotations

from datetime import datetime

from ._time import stamp
from .store import SqliteStore

_RECORD = """
INSERT INTO agent_comments (repo, comment_id, posted_at)
VALUES (:repo, :comment, :now)
ON CONFLICT(repo, comment_id) DO NOTHING
"""

_IDS_FOR = "SELECT comment_id FROM agent_comments WHERE repo = :repo"


class AgentComments:
    """Durable record of which comment ids belong to the agent."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def record(self, repo: str, comment_id: int, *, now: datetime) -> None:
        """Remember that the agent posted ``comment_id`` on ``repo``.

        Idempotent, because the publisher is retried: a second write of the
        same id is the same fact, not a conflict.
        """
        with self._store.transaction() as conn:
            conn.execute(
                _RECORD,
                {"repo": repo, "comment": comment_id, "now": stamp(now, "comment")},
            )

    def ids_for(self, repo: str) -> frozenset[int]:
        """Every comment id the agent has posted on ``repo``.

        Read once per poll cycle and handed to the classifier whole, the way
        the open pull requests are. The set is one row per review this agent
        has ever posted, which is bounded by what the budget allows and small
        enough to hold: a year of the busiest plausible deployment is tens of
        thousands of integers.
        """
        with self._store.transaction() as conn:
            return frozenset(row[0] for row in conn.execute(_IDS_FOR, {"repo": repo}))
