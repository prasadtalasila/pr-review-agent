"""The two things posted before a review exists: the 👀 and a refusal.

Both are courtesies, and a courtesy must never cost a review: a failed
acknowledgement does not raise, and a refusal notice says plainly that
nothing was charged.
"""

import json

import httpx
from publisher_harness import REPO, Transport, body_of, make_publisher, mention, opened

from pr_review_agent.publisher import refusal
from pr_review_agent.triggers.mention import has_mention
from pr_review_agent.triggers.models import CommentSource

# -- the acknowledgement -------------------------------------------------


async def test_a_conversation_mention_is_acknowledged_on_its_comment(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).acknowledge(mention())
    assert transport.paths == ["/repos/o/r/issues/comments/999/reactions"]


async def test_an_inline_mention_is_acknowledged_on_the_pulls_endpoint(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).acknowledge(
        mention(source=CommentSource.REVIEW)
    )
    assert transport.paths == ["/repos/o/r/pulls/comments/999/reactions"]


async def test_a_fresh_pull_request_is_acknowledged_on_itself(runs, posted):
    """Nobody wrote a comment to react to."""
    transport = Transport()
    await make_publisher(runs, posted, transport).acknowledge(opened())
    assert transport.paths == ["/repos/o/r/issues/7/reactions"]


async def test_the_acknowledgement_is_the_eyes_reaction(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).acknowledge(opened())
    assert json.loads(transport.requests[0].content) == {"content": "eyes"}


async def test_a_failed_acknowledgement_does_not_raise(runs, posted):
    """It is a courtesy; losing it must not cost a reserved review."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    await make_publisher(runs, posted, handler).acknowledge(opened())


async def test_a_dry_run_still_acknowledges(runs, posted):
    """The 👀 is not a publication: it says the agent has the trigger."""
    transport = Transport()
    await make_publisher(runs, posted, transport, dry_run=True).acknowledge(opened())
    assert transport.paths == ["/repos/o/r/issues/7/reactions"]


# -- the refusal notice --------------------------------------------------
#
# Issue #78: the 👀 goes on minutes before anyone knows a review is
# possible, so a deterministic refusal used to leave it as the last thing
# the agent ever said on that pull request.

NOTICE = "`max_changed_lines`: 6120 exceeds the configured 5000."


async def test_a_refusal_notice_is_posted_as_an_ordinary_comment(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).notify(opened(), NOTICE)

    assert transport.paths == ["/repos/o/r/issues/7/comments"]
    assert NOTICE in body_of(transport)


async def test_a_refusal_notice_says_nothing_was_charged(runs, posted):
    """The first thing a refused contributor wants to know."""
    transport = Transport()
    await make_publisher(runs, posted, transport).notify(opened(), NOTICE)
    assert "nothing was charged" in body_of(transport)


def test_a_refusal_notice_cannot_summon_the_review_it_explains():
    """Its own advice is a mention, on a pull request known to be unreviewable.

    The reader still sees the handle -- the entity renders as `@` -- but the
    raw body a later poll reads back has no `@` for `has_mention` to find.
    """
    body = refusal(NOTICE, handle="claude")
    assert "claude" in body
    assert not has_mention(body, "claude")


async def test_a_dry_run_posts_no_refusal_notice(runs, posted):
    """Unlike the 👀: this writes a comment, which is what the brake stops."""
    transport = Transport()
    await make_publisher(runs, posted, transport, dry_run=True).notify(opened(), NOTICE)
    assert transport.requests == []


async def test_a_failed_refusal_notice_does_not_raise(runs, posted):
    """The row it explains is already closed; letting this escape reopens it."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    await make_publisher(runs, posted, handler).notify(opened(), NOTICE)


async def test_a_refusal_notice_is_recorded_as_the_agents_own(runs, posted):
    """Every comment the agent posts, not only the ones a review is behind."""
    transport = Transport()
    await make_publisher(runs, posted, transport).notify(opened(), NOTICE)
    assert posted.ids_for(REPO) == frozenset({555})
