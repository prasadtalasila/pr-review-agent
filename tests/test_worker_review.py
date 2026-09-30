"""The review itself: what the engine is shown, and what is kept.

Round two is the case worth the fixture: a re-review is handed the previous
round's findings so a number survives it, and the checkout it ran against
does not.
"""

import json

from worker_harness import (
    NOW,
    POSIX_ONLY,
    PR,
    REPO,
    GitHubDouble,
    budget,
    mention,
    opened,
)

from pr_review_agent.budget import Mode
from pr_review_agent.engine import FakeEngine, Finding, Severity
from pr_review_agent.engine.models import Outcome
from pr_review_agent.findings import Assessment, Recommendation, Risk

pytestmark = POSIX_ONLY


# -- what one round hands the next ---------------------------------------


def finding(title="The handle leaks on the error path.", line=1):
    return Finding(
        path="feature.py",
        line=line,
        severity=Severity.MAJOR,
        title=title,
        body="b",
    )


async def _two_rounds(fixture, second_findings):
    """Drive one completed review, then a second with different findings."""
    fixture.queue.enqueue(opened(), now=NOW)
    await fixture.worker.run_once()
    assert isinstance(fixture.engine, FakeEngine)
    fixture.engine.findings = second_findings
    fixture.queue.enqueue(mention(), now=NOW)
    await fixture.worker.run_once()


async def test_the_engine_is_shown_the_previous_rounds_findings(wired):
    """Round 1 gets nothing; round 2 gets round 1's findings, numbered."""
    fixture = wired(engine=FakeEngine(findings=(finding(),)))
    await _two_rounds(fixture, (finding(),))

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    assert engine.requests[0].prior == ()
    assert [f.number for f in engine.requests[1].prior] == [1]


async def test_a_prior_finding_reaches_the_engine_without_its_body(wired):
    """The stripping is the control, so it is asserted where it is read."""
    fixture = wired(engine=FakeEngine(findings=(finding(),)))
    await _two_rounds(fixture, (finding(),))

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    (prior,) = engine.requests[1].prior
    assert prior.title == "The handle leaks on the error path."


async def test_recorded_findings_always_carry_a_number(wired):
    fixture = wired(engine=FakeEngine(findings=(finding(), finding(line=2))))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    recorded = fixture.runs.history(REPO, PR).prior
    assert [f.number for f in recorded] == [1, 2]


async def test_a_number_is_not_reused_after_its_finding_is_fixed(wired):
    """Round 1's item 1 is gone; the new finding must be 2, never 1."""
    fixture = wired(engine=FakeEngine(findings=(finding(),)))
    await _two_rounds(fixture, (finding(title="A different problem entirely."),))

    recorded = fixture.runs.history(REPO, PR).prior
    assert [f.number for f in recorded] == [2]


async def test_a_truncated_run_records_nothing_and_leaves_no_history(wired):
    fixture = wired(engine=FakeEngine(findings=(), outcome=Outcome.TRUNCATED))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.runs.history(REPO, PR).prior == ()


async def test_the_engine_is_handed_the_checkout_and_the_rung(wired, git_remote):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    (request,) = engine.requests
    assert request.mode is Mode.FULL
    assert request.trigger.dedupe_key == opened().dedupe_key
    assert request.checkout.head_sha == git_remote.head_sha
    assert "feature.py" in request.checkout.diff
    assert request.facts.number == PR


async def test_the_posted_review_names_what_the_exclusions_withheld(wired):
    """The shipped exclusions keep ``vendor/`` from the engine; the comment says so."""
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (comment,) = fixture.github.comments
    assert "`vendor/lib.js`._" in json.loads(comment.content)["body"]


async def test_the_posted_review_carries_the_engines_assessment(wired):
    """Issue #126: engine to run row to comment, through the real worker."""
    assessment = Assessment(
        effort=4,
        risk=Risk.MEDIUM,
        recommendation=Recommendation.MERGE_WITH_CAUTION,
        priority_files=("src/app.py",),
    )
    fixture = wired(engine=FakeEngine(findings=(finding(),), assessment=assessment))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (comment,) = fixture.github.comments
    assert (
        "**Effort** 4/5 · **Risk** medium · **Merge with caution** · "
        "Start with: `src/app.py`" in json.loads(comment.content)["body"]
    )


async def test_a_mention_is_reviewed_at_the_head_the_api_reports(wired, git_remote):
    """A mention's payload carries no head_sha; the facts read resolves it."""
    fixture = wired()
    fixture.queue.enqueue(mention(), now=NOW)

    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    (request,) = engine.requests
    assert request.trigger.head_sha is None
    assert request.checkout.head_sha == git_remote.head_sha


async def test_the_checkout_is_gone_once_the_review_is_over(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    (request,) = engine.requests
    assert not request.checkout.path.exists()


async def test_nothing_carries_from_one_run_to_the_next(wired):
    """Two runs, two trees. The tree under review is untrusted input."""
    fixture = wired()
    fixture.queue.enqueue(opened(head_sha="aaa"), now=NOW)
    fixture.queue.enqueue(opened(head_sha="bbb"), now=NOW)

    await fixture.worker.run_once()
    await fixture.worker.run_once()

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    first, second = engine.requests
    assert first.checkout.path != second.checkout.path
    assert not first.checkout.path.exists()


# -- issue #124: a later round is shown only what changed -----------------


def reviewed_since(store) -> list[str | None]:
    with store.transaction() as conn:
        return [
            row[0]
            for row in conn.execute("SELECT reviewed_since FROM ledger ORDER BY id")
        ]


async def _pushed_rounds(wired, git_remote, push):
    """Review once, let ``push`` move the head, then review again."""
    github = GitHubDouble(git_remote.head_sha)
    fixture = wired(engine=FakeEngine(findings=(finding(),)), github=github)
    fixture.queue.enqueue(opened(head_sha=git_remote.head_sha), now=NOW)
    await fixture.worker.run_once()
    head = push()
    github.move_head(head)
    fixture.queue.enqueue(opened(head_sha=head), now=NOW)
    await fixture.worker.run_once()
    assert isinstance(fixture.engine, FakeEngine)
    return fixture, fixture.engine.requests


async def test_a_later_round_is_shown_only_the_new_commits(
    wired, git_remote, contributor
):
    fixture, (first, second) = await _pushed_rounds(
        wired, git_remote, lambda: contributor.commit("fixup.py", "x = 1\n")
    )

    assert first.checkout.since_sha is None
    assert second.checkout.since_sha == git_remote.head_sha
    assert "fixup.py" in second.checkout.diff
    assert "feature.py" not in second.checkout.diff
    # The numbering invariant: prior is the whole of round one, not narrowed
    # to what the incremental diff happens to show.
    assert [(f.path, f.number) for f in second.prior] == [("feature.py", 1)]
    # And the ledger keeps the incremental round out of the pre-flight fit.
    assert reviewed_since(fixture.store) == [None, git_remote.head_sha]


async def test_a_force_push_is_compared_by_content(wired, git_remote, contributor):
    _, (_, second) = await _pushed_rounds(
        wired,
        git_remote,
        lambda: contributor.commit(
            "feature.py", "def added():\n    return 2\n", amend=True
        ),
    )

    assert second.checkout.since_sha == git_remote.head_sha
    assert "return 2" in second.checkout.diff
    assert "vendor/lib.js" not in second.checkout.diff


async def test_the_commit_threshold_makes_a_small_push_full(
    wired, git_remote, contributor
):
    config = budget(incremental_min_commits=2)
    github = GitHubDouble(git_remote.head_sha)
    fixture = wired(config=config, github=github)
    fixture.queue.enqueue(opened(head_sha=git_remote.head_sha), now=NOW)
    await fixture.worker.run_once()
    head = contributor.commit("fixup.py", "x = 1\n")
    github.move_head(head)
    fixture.queue.enqueue(opened(head_sha=head), now=NOW)
    await fixture.worker.run_once()

    assert isinstance(fixture.engine, FakeEngine)
    assert fixture.engine.requests[1].checkout.since_sha is None
    assert reviewed_since(fixture.store) == [None, None]
