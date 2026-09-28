"""Every way a run can fail, and what each one costs.

The taxonomy is the point: a failure before the engine settles nothing, an
engine that never started settles at a provable zero, and a pull request
that closed while the trigger sat in the queue is refused before it is paid
for.
"""

import logging
from dataclasses import replace
from datetime import timedelta

import pytest
from worker_harness import (
    MAX_RUN_TOKENS,
    NOW,
    POSIX_ONLY,
    PR,
    ExplodingEngine,
    GitHubDouble,
    UnstartableEngine,
    budget,
    client_returning,
    ledger_rows,
    opened,
    posted_comment,
    stop_reasons,
)

from pr_review_agent.budget import StopReason, UsageConfidence
from pr_review_agent.engine import FakeEngine
from pr_review_agent.queue import DEFAULT_LEASE, QueueStatus

pytestmark = POSIX_ONLY


# -- failure: what it settles at, and what it leaves behind --------------


async def test_an_engine_failure_settles_the_full_reservation(wired):
    """Anything may have been spent, and the governor cannot find out."""
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, engine, _ = row
    assert used == reserved == MAX_RUN_TOKENS
    assert confidence == str(UsageConfidence.UNAVAILABLE)
    assert engine == "exploding"


async def test_an_engine_that_never_started_settles_at_zero(wired):
    """No process existed, so no tokens were spent. Provable, not assumed."""
    fixture = wired(engine=UnstartableEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, engine, _ = row
    assert reserved == MAX_RUN_TOKENS
    assert (used, confidence) == (0, str(UsageConfidence.UNAVAILABLE))
    assert engine == "unstartable"


async def test_an_engine_that_never_started_is_its_own_stop_reason(wired):
    """A misconfigured host and a tool that fell over are different rows."""
    fixture = wired(engine=UnstartableEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert stop_reasons(fixture.store) == [str(StopReason.ENGINE_UNAVAILABLE)]


async def test_an_engine_that_never_started_is_retried_with_its_attempt_spent(
    wired,
):
    """Bounded: a binary that is missing now is missing on the next claim."""
    fixture = wired(engine=UnstartableEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING


async def test_an_engine_failure_is_retried(wired):
    fixture = wired(engine=ExplodingEngine())
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING


async def test_a_failure_before_the_engine_settles_nothing(wired):
    """Nothing reached an engine: that is provable, not assumed."""
    fixture = wired(client=client_returning(None, status=500))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    (row,) = ledger_rows(fixture.store)
    _, _, reserved, used, confidence, _, _ = row
    assert reserved == MAX_RUN_TOKENS
    assert (used, confidence) == (0, str(UsageConfidence.UNAVAILABLE))


async def test_a_client_failure_is_retried_with_its_attempt_spent(wired):
    fixture = wired(client=client_returning(None, status=500))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.PENDING
    again = fixture.queue.claim(now=NOW, owner="w2")
    assert again is not None and again.attempts == 2


async def test_an_oversized_pull_request_is_abandoned(wired):
    """Deterministic on this head: two more attempts reach the same refusal."""
    fixture = wired(config=budget(max_changed_files=0))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED
    (row,) = ledger_rows(fixture.store)
    assert row[3] == 0


async def test_an_unusable_payload_is_abandoned(wired):
    fixture = wired(client=client_returning({"number": PR}))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED


async def test_a_pull_request_closed_before_the_claim_is_never_reviewed(
    wired, git_remote
):
    """The leak this guard closes: a trigger outliving the pull request.

    The row was enqueued while the pull request was open and claimed after
    it was not. Nothing between those two moments asks GitHub, so without
    the check the agent clones, reviews and posts on something nobody will
    read -- at full price.
    """
    fixture = wired(github=GitHubDouble(git_remote.head_sha, pull_state="closed"))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.engine.requests == []
    # Refused *before* the checkout, so the mirror was never even cloned.
    assert not fixture.worker.workspace.mirror.exists()
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED
    (row,) = ledger_rows(fixture.store)
    assert (row[3], row[4]) == (0, str(UsageConfidence.EXACT))
    assert stop_reasons(fixture.store) == [str(StopReason.CLOSED)]


async def test_a_merged_pull_request_is_never_reviewed(wired, git_remote):
    """`merged` is read as well as `state`, and settles the same way."""
    fixture = wired(
        github=GitHubDouble(git_remote.head_sha, pull_state="open", merged=True)
    )
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.engine.requests == []
    assert not fixture.worker.workspace.mirror.exists()
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.ABANDONED
    (row,) = ledger_rows(fixture.store)
    assert (row[3], row[4]) == (0, str(UsageConfidence.EXACT))
    assert stop_reasons(fixture.store) == [str(StopReason.CLOSED)]


async def test_a_closed_pull_request_posts_nothing(wired, git_remote):
    """Refused before the spend, so there is no comment to post either."""
    fixture = wired(github=GitHubDouble(git_remote.head_sha, pull_state="closed"))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.github is not None
    assert fixture.github.comments == []


async def test_a_closed_pull_request_names_the_recovery_in_the_log(
    wired, git_remote, caplog
):
    """A reopen re-triggers nothing, so the line has to say what does.

    Every trigger is gated on a timestamp already in the past and the
    abandoned row's dedupe key blocks a second enqueue, so an operator who
    reopens the pull request and waits is waiting for nothing. A fresh
    ``@claude`` comment is what works.
    """
    fixture = wired(github=GitHubDouble(git_remote.head_sha, pull_state="closed"))
    fixture.queue.enqueue(opened(), now=NOW)

    with caplog.at_level(logging.INFO):
        await fixture.worker.run_once()

    assert "@claude" in caplog.text


async def test_a_merged_pull_request_is_offered_no_recovery(wired, git_remote, caplog):
    """Merged is terminal -- GitHub will not reopen it -- so the line says less."""
    fixture = wired(
        github=GitHubDouble(git_remote.head_sha, pull_state="closed", merged=True)
    )
    fixture.queue.enqueue(opened(), now=NOW)

    with caplog.at_level(logging.INFO):
        await fixture.worker.run_once()

    assert "merged before it was reviewed" in caplog.text
    assert "@claude" not in caplog.text


async def test_an_abandoned_trigger_is_never_offered_again(wired):
    fixture = wired(config=budget(max_changed_files=0))
    fixture.queue.enqueue(opened(), now=NOW)

    await fixture.worker.run_once()

    assert fixture.queue.claim(now=NOW, owner="w2") is None


# -- the lapsed lease ----------------------------------------------------


async def test_a_lapsed_worker_discards_its_run_before_starting(wired):
    """No reservation is held for it, so there is nothing to spend."""
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    assert claim is not None

    await fixture.worker.run_one(replace(claim, owner="somebody-else"))

    engine = fixture.engine
    assert isinstance(engine, FakeEngine)
    assert engine.requests == []
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.CLAIMED


async def test_a_lapsed_worker_settles_nothing(wired):
    fixture = wired()
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    assert claim is not None

    await fixture.worker.run_one(replace(claim, owner="somebody-else"))

    (row,) = ledger_rows(fixture.store)
    assert row[3] is None  # used_tokens: still unsettled, still charged


# -- the worker that crashed and came back as itself ---------------------


async def test_a_crashed_worker_reclaiming_its_own_row_still_publishes(
    wired, monkeypatch
):
    """Issue #70: one identity, two attempts, two unambiguous ledger rows.

    ``daemon.supervise`` restarts a crashed worker with the same ``owner``,
    so the retry reserved under the same ``(dedupe_key, owner)`` the crashed
    attempt had. Both open rows then matched one settle: it updated both,
    reported no single reservation, and the worker discarded a review it had
    already paid for -- a lost comment, now that every review posts its own
    rather than editing one in place.
    """
    reserved = 5_000
    fixture = wired(config=budget(max_run_tokens=reserved))
    fixture.queue.enqueue(opened(), now=NOW)
    claim = fixture.queue.claim(now=NOW, owner="worker-1", admit=fixture.governor.admit)
    assert claim is not None

    def died(*_args, **_kwargs):
        raise RuntimeError("the worker died holding its reservation")

    # After the engine, so the tokens really are spent, and of a type the
    # failure taxonomy has no answer for -- which is what makes it reach the
    # supervisor rather than being handed back as another attempt.
    record = fixture.runs.record
    monkeypatch.setattr(fixture.runs, "record", died)
    with pytest.raises(RuntimeError):
        await fixture.worker.run_one(claim)
    # Put it back by hand: `monkeypatch.undo()` would also undo the
    # `GIT_SSL_CAINFO` the workspace fixture set, and the retry needs it.
    monkeypatch.setattr(fixture.runs, "record", record)

    # The row is still claimed under a live lease and the reservation is
    # still open: that is the state the restarted worker walks back into.
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.CLAIMED
    assert [row[3] for row in ledger_rows(fixture.store)] == [None]

    retry = fixture.queue.claim(
        now=NOW + DEFAULT_LEASE + timedelta(seconds=1),
        owner="worker-1",
        admit=fixture.governor.admit,
    )
    assert retry is not None and retry.owner == claim.owner

    await fixture.worker.run_one(retry)

    # The review it paid for is posted, not thrown away.
    assert posted_comment(fixture.store, opened().dedupe_key) == 555
    assert fixture.queue.status(opened().dedupe_key) is QueueStatus.DONE
    # One settled row per attempt. The crashed one is charged its whole
    # ceiling -- nobody can say what it spent -- and the one that ran is
    # charged what the engine reported, so neither overwrites the other.
    assert [(row[2], row[3]) for row in ledger_rows(fixture.store)] == [
        (reserved, reserved),
        (reserved, 1_000),
    ]
    assert stop_reasons(fixture.store) == [str(StopReason.LOST), "completed"]
