"""What one poll cycle sees, enqueues, and remembers it saw.

The watermark is the whole of the cold-start protection and most of the
duplicate protection, so it is asserted beside the classification it bounds
rather than in a file of its own: an item is enqueued once, the watermark
moves to the newest item rather than to `now`, and an endpoint that did not
change moves nothing.
"""

from datetime import timedelta

import httpx
import pytest
from daemon_harness import (
    COMMENTS_WM,
    CONFIG,
    NOW,
    OLD,
    OTHER,
    OTHER_PULLS_WM,
    PULLS_WM,
    RECENT,
    SEEN,
    issue_comment,
    make_daemon,
    pr_item,
    queued,
    responder,
    review_comment,
)

from pr_review_agent.comments import AgentComments
from pr_review_agent.daemon import COMMENTS, EMPTY, PULL_REQUESTS
from pr_review_agent.poller.endpoints import Endpoint
from pr_review_agent.queue import ReviewQueue
from pr_review_agent.store import SqliteStore


async def test_cold_start_enqueues_nothing_from_the_backlog(tmp_path):
    # The whole point of seeding: a fresh database must not pay to review
    # every open pull request and replay every historical @claude.
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(1, OLD), pr_item(2, OLD + timedelta(days=1))],
            issue_comments=[issue_comment(11, OLD), issue_comment(12, RECENT)],
            review_comments=[review_comment(13, RECENT)],
        ),
    )
    daemon.seed_watermarks(now=NOW)

    summary = await daemon.run_once()

    assert summary.enqueued == 0
    assert queued(daemon) == 0


async def test_seeding_sets_both_watermarks(tmp_path):
    daemon = make_daemon(tmp_path, responder())
    daemon.seed_watermarks(now=NOW)
    assert daemon.store.watermark(PULLS_WM) == NOW
    assert daemon.store.watermark(COMMENTS_WM) == NOW


async def test_seeding_forgets_the_pulls_etag(tmp_path):
    # The set of open pull requests is in memory and a restart empties it;
    # the ETag that would refill it is in SQLite and a restart does not.
    # Left alone, the daemon sends the stored ETag, gets a 304, and runs
    # with the comment filter off until some open pull request changes.
    daemon = make_daemon(
        tmp_path,
        responder(pulls=[pr_item(1, RECENT)], issue_comments=[]),
    )
    await daemon.run_once()
    pulls_path = daemon.poller.endpoints.path(Endpoint.OPEN_PULLS)
    assert daemon.store.get(pulls_path) is not None

    daemon.seed_watermarks(now=NOW)

    assert daemon.store.get(pulls_path) is None
    assert daemon.store.get(daemon.poller.endpoints.path(Endpoint.ISSUE_COMMENTS))


async def test_a_restart_fills_the_open_set_before_the_first_304(tmp_path):
    """What the forgotten ETag buys: the filter is on from the first cycle.

    The restarted daemon is given a store that already holds every ETag,
    and a GitHub that answers 304 to anything conditional. Without the
    forget, the open set would stay ``None`` -- the filter off -- for as
    long as no open pull request changed.
    """
    store = SqliteStore(tmp_path / "state.db")
    first = make_daemon(tmp_path, responder(pulls=[pr_item(1, RECENT)]), store=store)
    await first.run_once()

    def only_unconditional(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, json=[pr_item(1, RECENT)], headers={"etag": '"e"'})

    restarted = make_daemon(tmp_path, only_unconditional, store=store)
    restarted.seed_watermarks(now=NOW)

    await restarted.run_once()
    assert restarted.open_pull_requests == frozenset({1})

    # The second cycle is conditional again and answers 304, and the set
    # survives it: a 304 means unchanged, not unknown.
    await restarted.run_once()
    assert restarted.open_pull_requests == frozenset({1})


async def test_seeding_does_not_rewind_an_existing_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULLS_WM, NOW)
    daemon.seed_watermarks(now=OLD)
    assert daemon.store.watermark(PULLS_WM) == NOW


async def test_two_repositories_on_one_store_keep_separate_watermarks(tmp_path):
    # Why the key carries the repository: one store is how several daemons
    # share one budget, and an unqualified key would let whichever polled
    # last overwrite the rest -- every other repository then reading its own
    # backlog as already seen, and skipping it forever.
    store = SqliteStore(tmp_path / "state.db")
    mine = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]), store=store)
    theirs = make_daemon(tmp_path, responder(), config=OTHER, store=store)
    theirs.seed_watermarks(now=NOW)
    mine.store.advance_watermark(PULLS_WM, OLD)

    summary = await mine.run_once()

    assert summary.enqueued == 1
    assert store.watermark(PULLS_WM) == RECENT
    assert store.watermark(OTHER_PULLS_WM) == NOW


async def test_an_unqualified_watermark_is_adopted(tmp_path):
    # A database written before the key carried a repository. Dropping the
    # value re-offers the whole open backlog as new; seeding over it skips
    # every event in flight. Neither is acceptable, so it is carried across.
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULL_REQUESTS, RECENT)
    daemon.store.advance_watermark(COMMENTS, RECENT)

    daemon.seed_watermarks(now=NOW)

    assert daemon.store.watermark(PULLS_WM) == RECENT
    assert daemon.store.watermark(COMMENTS_WM) == RECENT


async def test_an_unqualified_watermark_is_adopted_only_once(tmp_path):
    # A legacy row written again after the adoption -- by a downgraded
    # binary, say -- must not be read a second time. Here it has moved ahead
    # of the qualified one, so a second adoption would show.
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)
    daemon.seed_watermarks(now=NOW)
    assert daemon.store.watermark(PULLS_WM) == OLD
    daemon.store.advance_watermark(PULL_REQUESTS, NOW + timedelta(days=1))

    daemon.seed_watermarks(now=NOW)

    assert daemon.store.watermark(PULLS_WM) == OLD


async def test_a_pull_request_after_the_watermark_is_enqueued(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULLS_WM, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 1
    assert daemon.queue.status("pr_opened:o/r:3:sha3") is not None


async def test_repolling_the_same_payload_enqueues_once(tmp_path):
    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.store.advance_watermark(PULLS_WM, OLD)

    first = await daemon.run_once()
    # Rewind by hand: the watermark alone would hide the second look, and
    # the dedupe key is what this test is about.
    daemon.store.advance_watermark(PULLS_WM, OLD)
    second = await daemon.run_once()

    assert (first.enqueued, second.enqueued) == (1, 0)
    assert queued(daemon) == 1


async def test_watermark_advances_to_the_newest_item_not_to_now(tmp_path):
    older = RECENT - timedelta(hours=1)
    daemon = make_daemon(
        tmp_path, responder(pulls=[pr_item(3, RECENT), pr_item(4, older)])
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)

    await daemon.run_once()

    assert daemon.store.watermark(PULLS_WM) == RECENT


async def test_an_unchanged_endpoint_moves_no_watermark(tmp_path):
    daemon = make_daemon(tmp_path, responder())  # every endpoint 304s
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    summary = await daemon.run_once()

    assert summary == EMPTY
    assert daemon.store.watermark(PULLS_WM) == OLD
    assert daemon.store.watermark(COMMENTS_WM) == OLD


async def test_both_comment_endpoints_share_one_watermark(tmp_path):
    daemon = make_daemon(
        tmp_path,
        responder(
            issue_comments=[issue_comment(11, RECENT - timedelta(hours=2))],
            review_comments=[review_comment(12, RECENT)],
        ),
    )
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 2
    assert daemon.store.watermark(COMMENTS_WM) == RECENT


# -- the agent's own comments ---------------------------------------------
#
# There is no check on which account the agent posts as (issue #36). What
# stops it answering itself is the set of comment ids it recorded when it
# posted them, which this leg reads once per cycle (issue #108).


async def test_a_comment_the_agent_posted_is_not_enqueued(tmp_path):
    """Even carrying a live mention: this is the case `neutralise` misses."""
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(12, SEEN)],
            issue_comments=[issue_comment(11, RECENT)],
        ),
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)
    AgentComments(daemon.store).record(CONFIG.github.repo, 11, now=RECENT)

    summary = await daemon.run_once()

    assert summary.enqueued == 0
    assert queued(daemon) == 0


async def test_a_contributors_comment_in_the_same_cycle_is_enqueued(tmp_path):
    """The set rejects the agent's comment, not the pull request it is on."""
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(12, SEEN)],
            issue_comments=[issue_comment(11, RECENT), issue_comment(13, RECENT)],
        ),
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)
    AgentComments(daemon.store).record(CONFIG.github.repo, 11, now=RECENT)

    summary = await daemon.run_once()

    assert summary.enqueued == 1


async def test_another_repositorys_recorded_comment_does_not_apply(tmp_path):
    """One store backs several repositories; a comment id is not unique."""
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(12, SEEN)],
            issue_comments=[issue_comment(11, RECENT)],
        ),
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)
    AgentComments(daemon.store).record("other/repo", 11, now=RECENT)

    assert (await daemon.run_once()).enqueued == 1


# -- comments on closed pull requests -------------------------------------
#
# Both comment endpoints are repo-wide, so they return comments on pull
# requests closed weeks ago. The open set comes off the `/pulls` leg of the
# same sweep and has to survive that leg answering 304.


async def test_a_comment_on_a_closed_pull_request_is_not_enqueued(tmp_path):
    # The comment names pr 12; the only open pull request is 3.
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(3, SEEN)],
            issue_comments=[issue_comment(11, RECENT)],
        ),
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    summary = await daemon.run_once()

    assert summary.enqueued == 0
    assert queued(daemon) == 0


async def test_a_comment_on_a_pull_request_opened_this_cycle_is_enqueued(tmp_path):
    # Both legs belong to one sweep, and the pulls leg is classified first.
    daemon = make_daemon(
        tmp_path,
        responder(
            pulls=[pr_item(12, SEEN)],
            issue_comments=[issue_comment(11, RECENT)],
        ),
    )
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    assert (await daemon.run_once()).enqueued == 1


async def test_the_open_pull_request_set_survives_a_304(tmp_path):
    """A 304 on the pulls leg means unchanged, not unknown."""
    cycles = iter(
        [
            # First: pr 12 is open, no comments yet.
            responder(pulls=[pr_item(12, SEEN)]),
            # Second: the pulls leg 304s, and the mention arrives.
            responder(issue_comments=[issue_comment(11, RECENT)]),
        ]
    )
    handler = next(cycles)

    def dispatch(request):
        return handler(request)

    daemon = make_daemon(tmp_path, dispatch)
    daemon.store.advance_watermark(PULLS_WM, OLD)
    daemon.store.advance_watermark(COMMENTS_WM, OLD)

    await daemon.run_once()
    handler = next(cycles)

    assert (await daemon.run_once()).enqueued == 1


async def test_a_failing_enqueue_leaves_the_watermark_unmoved(tmp_path):
    # Enqueue happens before the watermark advances, so a crash in between
    # costs one re-classification rather than a lost trigger.
    class BrokenQueue(ReviewQueue):
        """A queue whose writes fail outright."""

        def enqueue(self, trigger, *, now):
            raise RuntimeError("disk full")

    daemon = make_daemon(tmp_path, responder(pulls=[pr_item(3, RECENT)]))
    daemon.queue = BrokenQueue(daemon.store, repo=daemon.config.github.repo)
    daemon.store.advance_watermark(PULLS_WM, OLD)

    with pytest.raises(RuntimeError):
        await daemon.run_once()

    assert daemon.store.watermark(PULLS_WM) == OLD


async def test_adoption_deletes_the_legacy_rows(tmp_path):
    """Deleted in the same transaction that adopts them.

    Leaving them inert was the earlier choice, so that a downgrade still
    found its watermark. It cannot survive a shared store: the rows outlive
    the repository they describe, and the next repository to arrive would
    read them as its own.
    """
    daemon = make_daemon(tmp_path, responder())
    daemon.store.advance_watermark(PULL_REQUESTS, OLD)
    daemon.store.advance_watermark(COMMENTS, OLD)

    daemon.seed_watermarks(now=NOW)

    assert daemon.store.watermark(PULLS_WM) == OLD
    assert daemon.store.watermark(PULL_REQUESTS) is None
    assert daemon.store.watermark(COMMENTS) is None


async def test_a_second_repository_does_not_adopt_the_firsts_watermark(tmp_path):
    """The legacy rows belong to the repository that upgraded, and to it only.

    A newcomer pointed at that store would otherwise start from a timestamp
    it has never polled -- possibly weeks back -- and enqueue and pay for
    every open pull request since. That is the cold-start spend bound, lost
    to a rename.
    """
    store = SqliteStore(tmp_path / "state.db")
    store.advance_watermark(PULL_REQUESTS, OLD)
    mine = make_daemon(tmp_path, responder(), store=store)
    mine.seed_watermarks(now=NOW)
    assert store.watermark(PULLS_WM) == OLD

    theirs = make_daemon(tmp_path, responder(), config=OTHER, store=store)
    theirs.seed_watermarks(now=NOW)

    assert store.watermark(OTHER_PULLS_WM) == NOW
