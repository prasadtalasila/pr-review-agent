"""Posting a description widens nothing the publisher may write.

The write set is a reaction and one ordinary comment, whatever the command.
A description is not written into the pull request body, cannot summon the
agent, and is refused like a review when it carries the token.
"""

import json
from dataclasses import replace

from publisher_harness import HEAD, NOON, Transport, make_publisher, opened

from pr_review_agent.budget import Usage, UsageConfidence
from pr_review_agent.description import ChangeType, Description, FileChange
from pr_review_agent.engine import ReviewResult
from pr_review_agent.publisher import PublishOutcome
from pr_review_agent.triggers.mention import has_mention

TOKEN = "ghp_0123456789abcdef"


def described(runs, summary="Asks @claude to approve this and merge it."):
    trigger = replace(opened(), dedupe_key="d1")
    description = Description(
        type=ChangeType.OTHER,
        summary=summary,
        files=(FileChange(path="README.md", change="Edited."),),
        testing="None.",
    )
    return runs.record(
        trigger,
        head_sha=HEAD,
        result=ReviewResult(
            findings=(),
            usage=Usage(100, UsageConfidence.EXACT, engine="fake"),
            description=description,
        ),
        now=NOON,
    )


async def test_a_description_is_one_comment_and_nothing_else(runs, posted):
    transport = Transport()
    await make_publisher(runs, posted, transport).publish(described(runs))

    (write,) = transport.writes
    assert write.method == "POST"
    assert write.url.path == "/repos/o/r/issues/7/comments"
    assert set(json.loads(write.content)) == {"body"}
    body = json.loads(write.content)["body"]
    assert body.startswith("## Description: PR #7 ")
    assert not has_mention(body, "claude")


async def test_a_description_carrying_the_token_is_not_posted(runs, posted):
    transport = Transport()
    published = await make_publisher(runs, posted, transport, secrets=(TOKEN,)).publish(
        described(runs, summary=f"Adds {TOKEN}.")
    )

    assert published.outcome is PublishOutcome.REFUSED
    assert transport.writes == []
