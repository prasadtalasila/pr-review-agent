"""A description is a run, so it can be re-posted -- and is never a round."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.budget import Usage, UsageConfidence
from pr_review_agent.description import ChangeType, Description, FileChange
from pr_review_agent.engine import Finding, ReviewResult, Severity
from pr_review_agent.runs import RunStore
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import Command, Trigger, TriggerKind

NOON = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
USAGE = Usage(10, UsageConfidence.EXACT, engine="fake")
DESCRIPTION = Description(
    type=ChangeType.TESTS,
    summary="Adds tests.",
    files=(FileChange(path="tests/t.py", change="New."),),
    testing="pytest",
)
REVIEW = Trigger(
    kind=TriggerKind.PR_OPENED,
    repo="o/r",
    pr_number=7,
    head_sha="aaa",
    actor_id=1,
    dedupe_key="pr_opened:o/r:7:aaa",
)
DESCRIBE = replace(
    REVIEW,
    kind=TriggerKind.MENTION,
    dedupe_key="mention:o/r:7:5",
    command=Command.DESCRIBE,
)
FINDING = Finding(
    path="a.py", line=1, severity=Severity.MAJOR, title="t", body="b", number=1
)


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield store


@pytest.fixture(name="runs")
def runs_fixture(store):
    return RunStore(store)


def _record(runs: RunStore, at: datetime) -> None:
    runs.record(
        REVIEW,
        head_sha="aaa",
        result=ReviewResult(findings=(FINDING,), usage=USAGE),
        now=at,
    )
    runs.record(
        DESCRIBE,
        head_sha="bbb",
        result=ReviewResult(findings=(), usage=USAGE, description=DESCRIPTION),
        now=at + timedelta(minutes=1),
    )


def test_an_unposted_description_comes_back_whole(runs):
    _record(runs, NOON)
    pending = runs.unpublished(DESCRIBE.dedupe_key)
    assert pending is not None
    assert pending.description == DESCRIPTION
    assert runs.unpublished(REVIEW.dedupe_key).description is None


def test_a_description_is_not_the_last_round(runs):
    _record(runs, NOON)
    history = runs.history("o/r", 7)
    assert history.prior == (FINDING,)
    assert history.head_sha == "aaa"
    assert runs.round_of("o/r", 7, REVIEW.dedupe_key) == 1


def test_a_purged_description_stays_a_description(store, runs):
    _record(runs, NOON)
    assert runs.purge_content("o/r", 7, now=NOON + timedelta(hours=1)) == 2
    assert runs.unpublished(DESCRIBE.dedupe_key) is None
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT dedupe_key, description FROM runs ORDER BY dedupe_key"
        ).fetchall()
    assert rows == [(DESCRIBE.dedupe_key, "{}"), (REVIEW.dedupe_key, None)]
