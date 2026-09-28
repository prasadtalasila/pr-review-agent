"""Publishing a review: the live head re-check, one comment, and the record.

A review describes one commit, so the head is re-read immediately before
posting; a superseded head is posted once and stamped so it is never
offered again; and the comment id is written down rather than rewritten.
"""

import json

import httpx
import pytest
from publisher_harness import (
    FINDINGS,
    HEAD,
    REPO,
    Transport,
    body_of,
    make_publisher,
    recorded,
)

from pr_review_agent.poller.client import GitHubClientError
from pr_review_agent.publisher import PublishOutcome

# -- the head re-check ---------------------------------------------------


async def test_a_superseded_head_is_posted_and_says_so(runs, posted):
    """The tokens are spent by the time the head is re-read.

    Discarding the review saves nothing and shows nobody anything, so it is
    posted with a line naming the commit it actually describes.
    """
    transport = Transport(head="a-newer-commit")
    outcome = await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert outcome.outcome is PublishOutcome.SUPERSEDED
    body = json.loads(transport.writes[0].content)["body"]
    assert "no longer the head" in body
    assert "a-newer" in body


async def test_post_superseded_false_restores_the_discard(runs, posted):
    transport = Transport(head="a-newer-commit")
    publisher = make_publisher(runs, posted, transport, post_superseded=False)
    outcome = await publisher.publish(recorded(runs))
    assert outcome.outcome is PublishOutcome.SUPERSEDED
    assert transport.writes == []


@pytest.mark.parametrize("post_superseded", [True, False])
async def test_a_superseded_run_is_stamped_and_never_offered_again(
    runs, posted, post_superseded
):
    """Issue #68: the discard path used to leave `published_at` NULL forever.

    An unstamped run is a run the queue keeps offering for publication, and
    every claim that took the offer re-read the same moved head. Whether the
    review was posted or discarded, the run is finished.
    """
    transport = Transport(head="a-newer-commit")
    publisher = make_publisher(runs, posted, transport, post_superseded=post_superseded)
    run = recorded(runs)
    await publisher.publish(run)
    assert runs.unpublished(run.dedupe_key) is None


async def test_a_matching_head_publishes(runs, posted):
    transport = Transport()
    outcome = await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert outcome.outcome is PublishOutcome.PUBLISHED
    assert outcome.comment_id == 555


async def test_the_head_is_read_live_rather_than_trusted(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert transport.paths[0] == "/repos/o/r/pulls/7"


async def test_an_unreadable_pull_request_payload_raises(runs, posted):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"no": "head"})

    with pytest.raises(Exception, match="head"):
        await make_publisher(runs, posted, handler).publish(recorded(runs))


# -- one comment per pull request ----------------------------------------


async def test_a_first_review_posts_a_new_comment(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert transport.writes[0].method == "POST"
    assert transport.writes[0].url.path == "/repos/o/r/issues/7/comments"


async def test_a_re_review_posts_a_second_comment(runs, posted):
    """One comment per review since 1.3.0, and nothing is ever edited."""
    transport = Transport()
    publisher = make_publisher(runs, posted, transport)
    first = await publisher.publish(recorded(runs, key="first"))
    second = await publisher.publish(recorded(runs, key="second"))
    assert [w.method for w in transport.writes] == ["POST", "POST"]
    assert transport.writes[1].url.path == "/repos/o/r/issues/7/comments"
    assert (first.comment_id, second.comment_id) == (555, 556)


async def test_a_clean_re_review_posts_its_own_comment(runs, posted):
    transport = Transport()
    publisher = make_publisher(runs, posted, transport)
    await publisher.publish(recorded(runs, key="first"))
    await publisher.publish(recorded(runs, findings=(), key="second"))
    assert [w.method for w in transport.writes] == ["POST", "POST"]


async def test_a_deleted_comment_cannot_strand_a_review(runs, posted):
    """Issue #71: nothing dereferences the id a maintainer may have deleted."""
    transport = Transport()
    publisher = make_publisher(runs, posted, transport)
    await publisher.publish(recorded(runs, key="first"))
    await publisher.publish(recorded(runs, key="second"))
    assert not [w for w in transport.writes if w.method == "PATCH"]
    assert "/issues/comments/" not in " ".join(transport.paths)


async def test_the_comment_it_posts_is_remembered(runs, posted):
    """Issue #108: what the classifier reads to know its own words."""
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert posted.ids_for(REPO) == frozenset({555})


async def test_every_round_is_remembered(runs, posted):
    transport = Transport()
    publisher = make_publisher(runs, posted, transport)
    await publisher.publish(recorded(runs, key="first"))
    await publisher.publish(recorded(runs, key="second"))
    assert posted.ids_for(REPO) == frozenset({555, 556})


async def test_a_dry_run_remembers_nothing(runs, posted):
    """It posted no comment, so there is no comment of its own to know."""
    transport = Transport()
    await make_publisher(runs, posted, transport, dry_run=True).publish(recorded(runs))
    assert posted.ids_for(REPO) == frozenset()


async def test_a_failed_post_remembers_nothing(runs, posted):
    """Nothing was said, so the agent has nothing to disown later."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"head": {"sha": HEAD}, "commits": 3})
        return httpx.Response(502, json={"message": "bad gateway"})

    with pytest.raises(GitHubClientError):
        await make_publisher(runs, posted, handler).publish(recorded(runs))
    assert posted.ids_for(REPO) == frozenset()


async def test_publishing_stamps_the_run(runs, posted):
    transport = Transport()
    published = await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert runs.unpublished("k1") is None
    assert published.comment_id == 555


# -- what the comment says -----------------------------------------------


async def test_a_clean_review_says_so(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs, findings=()))
    assert "No issues found" in body_of(transport)


async def test_a_findings_headline_and_body_are_both_rendered(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    body = body_of(transport)
    assert "The file handle leaks when parsing raises." in body
    assert "leaks a handle" in body


async def test_a_finding_carries_no_path_line_anchor(runs, posted):
    """Deliberate: the reference report names paths in prose, not in an anchor."""
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert "src/a.py:12" not in body_of(transport)


async def test_findings_are_ordered_by_section_then_number(runs, posted):
    """A re-review of the same findings must render identically."""
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    body = body_of(transport)
    assert body.index("## Should fix") < body.index("## Nits")


async def test_the_same_findings_render_identically_twice(runs, posted):
    """Only the round differs: the findings below the header must not move.

    The header legitimately changes -- a second review is round 2 -- so the
    no-op-diff property is asserted on everything under it, which is what
    stable ordering actually protects.
    """
    transport = Transport()
    publisher = make_publisher(runs, posted, transport)
    await publisher.publish(recorded(runs, key="first"))
    await publisher.publish(recorded(runs, findings=FINDINGS[::-1], key="second"))
    bodies = [json.loads(w.content)["body"] for w in transport.writes]
    assert bodies[0].split("\n", 1)[1] == bodies[1].split("\n", 1)[1]
    assert "round 1" in bodies[0] and "round 2" in bodies[1]


async def test_the_comment_names_the_commit_it_reviewed(runs, posted):
    """An edited comment otherwise says nothing about which head it describes."""
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(recorded(runs))
    assert HEAD[:7] in body_of(transport)
