"""RunStore: what a paid review produced, so publishing can be retried."""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.budget import Usage, UsageConfidence
from pr_review_agent.engine import Finding, Outcome, ReviewResult, Severity
from pr_review_agent.runs import RunStore
from pr_review_agent.store import SqliteStore
from pr_review_agent.triggers.models import Trigger, TriggerKind

NOON = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
LATER = NOON + timedelta(minutes=5)
REPO = "o/r"
HEAD = "deadbeef"

FINDINGS = (
    Finding(
        path="src/a.py",
        line=12,
        severity=Severity.MAJOR,
        title="The file handle leaks when parsing raises.",
        body="leaks a handle",
    ),
    Finding(
        path="src/b.py",
        line=3,
        severity=Severity.NIT,
        title="A stray space trails the assignment.",
        body="stray space",
    ),
)


def numbered_finding(number, body="b", severity=Severity.MAJOR):
    return Finding(
        path="src/a.py",
        line=12,
        severity=severity,
        title="The handle leaks on the error path.",
        body=body,
        number=number,
    )


def trigger(pr=7, key="pr_opened:o/r:7:deadbeef"):
    return Trigger(
        kind=TriggerKind.PR_OPENED,
        repo=REPO,
        pr_number=pr,
        head_sha=HEAD,
        actor_id=99,
        dedupe_key=key,
    )


def result(findings=FINDINGS, outcome=Outcome.COMPLETED):
    return ReviewResult(
        findings=findings,
        usage=Usage(100, UsageConfidence.EXACT, engine="fake"),
        outcome=outcome,
    )


@pytest.fixture(name="runs")
def runs_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield RunStore(store)


def test_a_recorded_run_round_trips(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    recorded = runs.unpublished_for(REPO, 7)
    assert recorded.dedupe_key == "pr_opened:o/r:7:deadbeef"
    assert recorded.head_sha == HEAD
    assert recorded.outcome is Outcome.COMPLETED
    assert recorded.findings == FINDINGS


def test_a_clean_run_records_no_findings(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(findings=()), now=NOON)
    assert runs.unpublished_for(REPO, 7).findings == ()


def test_recording_the_same_run_twice_refreshes_it(runs):
    """A resumed claim re-records rather than raising."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.record(trigger(), head_sha="newer", result=result(findings=()), now=LATER)
    recorded = runs.unpublished_for(REPO, 7)
    assert recorded.head_sha == "newer"
    assert recorded.findings == ()


def test_a_published_run_is_not_offered_again(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published("pr_opened:o/r:7:deadbeef", comment_id=555, now=LATER)
    assert runs.unpublished_for(REPO, 7) is None


def test_an_unpublished_run_for_another_pull_request_is_not_offered(runs):
    runs.record(trigger(pr=8, key="k8"), head_sha=HEAD, result=result(), now=NOON)
    assert runs.unpublished_for(REPO, 7) is None


def test_the_oldest_unpublished_run_is_offered_first(runs):
    runs.record(trigger(key="first"), head_sha=HEAD, result=result(), now=NOON)
    runs.record(trigger(key="second"), head_sha=HEAD, result=result(), now=LATER)
    assert runs.unpublished_for(REPO, 7).dedupe_key == "first"


def test_marking_published_records_the_comment(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published("pr_opened:o/r:7:deadbeef", comment_id=555, now=LATER)
    assert runs.comment_for_pull_request(REPO, 7) == 555


def test_a_pull_request_with_no_comment_yet(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    assert runs.comment_for_pull_request(REPO, 7) is None


def test_the_newest_comment_is_the_one_edited_in_place(runs):
    """One agent comment per pull request, rewritten on re-review."""
    runs.record(trigger(key="first"), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published("first", comment_id=555, now=NOON)
    runs.record(trigger(key="second"), head_sha=HEAD, result=result(), now=LATER)
    runs.mark_published("second", comment_id=555, now=LATER)
    assert runs.comment_for_pull_request(REPO, 7) == 555


def test_another_pull_requests_comment_is_not_reused(runs):
    runs.record(trigger(pr=8, key="k8"), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published("k8", comment_id=999, now=NOON)
    assert runs.comment_for_pull_request(REPO, 7) is None


def test_purging_empties_the_content_and_keeps_the_rest(runs):
    """The ledger survives the purge, and so does the comment id."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published("pr_opened:o/r:7:deadbeef", comment_id=555, now=NOON)
    assert runs.purge_content(REPO, 7, now=LATER) == 1
    assert runs.comment_for_pull_request(REPO, 7) == 555


def test_purging_twice_purges_nothing_the_second_time(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.purge_content(REPO, 7, now=LATER)
    assert runs.purge_content(REPO, 7, now=LATER) == 0


def test_a_purged_run_is_never_offered_for_publication(runs):
    """Its findings are gone, so there is nothing left to post."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.purge_content(REPO, 7, now=LATER)
    assert runs.unpublished_for(REPO, 7) is None


def test_a_naive_timestamp_is_refused(runs):
    with pytest.raises(ValueError, match="timezone-aware"):
        runs.record(
            trigger(), head_sha=HEAD, result=result(), now=datetime(2026, 9, 18, 12)
        )


def test_has_unpublished_sees_a_waiting_run(runs):
    """Asked inside the claim transaction, so it takes the connection."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    with runs._store.transaction() as conn:  # noqa: SLF001
        assert runs.has_unpublished(conn, REPO, 7) is True


def test_has_unpublished_is_false_once_published(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published("pr_opened:o/r:7:deadbeef", comment_id=555, now=LATER)
    with runs._store.transaction() as conn:  # noqa: SLF001
        assert runs.has_unpublished(conn, REPO, 7) is False


def test_has_unpublished_is_false_for_another_pull_request(runs):
    runs.record(trigger(pr=8, key="k8"), head_sha=HEAD, result=result(), now=NOON)
    with runs._store.transaction() as conn:  # noqa: SLF001
        assert runs.has_unpublished(conn, REPO, 7) is False


def test_has_unpublished_ignores_a_purged_run(runs):
    """Its findings are gone, so it is not work waiting to be posted."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.purge_content(REPO, 7, now=LATER)
    with runs._store.transaction() as conn:  # noqa: SLF001
        assert runs.has_unpublished(conn, REPO, 7) is False


def test_a_findings_title_and_number_survive_the_round_trip(runs):
    numbered = (
        Finding(
            path="src/a.py",
            line=12,
            severity=Severity.MAJOR,
            title="The handle leaks on the error path.",
            body="`open()` at line 12 is not closed when `parse` raises.",
            number=3,
        ),
    )
    runs.record(trigger(), head_sha=HEAD, result=result(numbered), now=NOON)
    assert runs.unpublished_for(REPO, 7).findings == numbered


def test_a_finding_stored_before_titles_existed_still_loads(runs):
    """A row written by an older build has no title and no number."""
    runs.record(trigger(), head_sha=HEAD, result=result(()), now=NOON)
    legacy = '[{"path": "src/a.py", "line": 12, "severity": "major", "body": "old"}]'
    with runs._store.transaction() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE runs SET findings = :f WHERE repo = :r AND pr_number = :p",
            {"f": legacy, "r": REPO, "p": 7},
        )
    assert runs.unpublished_for(REPO, 7).findings == (
        Finding(
            path="src/a.py",
            line=12,
            severity=Severity.MAJOR,
            title="",
            body="old",
            number=None,
        ),
    )


# -- cross-round history --


def test_a_first_review_has_no_history(runs):
    history = runs.history(REPO, 7)
    assert history.prior == ()
    assert history.high_water == 0


def test_history_returns_the_newest_completed_rounds_findings(runs):
    first = (numbered_finding(number=1, body="round one"),)
    second = (numbered_finding(number=1, body="round two"),)
    runs.record(trigger(key="k1"), head_sha=HEAD, result=result(first), now=NOON)
    runs.record(trigger(key="k2"), head_sha=HEAD, result=result(second), now=LATER)
    assert runs.history(REPO, 7).prior == second


def test_the_high_water_mark_is_the_largest_number_ever_issued(runs):
    """Item 4 was fixed in round 2; its number must still not be reused."""
    runs.record(
        trigger(key="k1"),
        head_sha=HEAD,
        result=result((numbered_finding(number=4),)),
        now=NOON,
    )
    runs.record(
        trigger(key="k2"),
        head_sha=HEAD,
        result=result((numbered_finding(number=1),)),
        now=LATER,
    )
    assert runs.history(REPO, 7).high_water == 4


def test_a_truncated_round_contributes_no_prior_findings(runs):
    runs.record(trigger(key="k1"), head_sha=HEAD, result=result(), now=NOON)
    runs.record(
        trigger(key="k2"),
        head_sha=HEAD,
        result=result((), outcome=Outcome.TRUNCATED),
        now=LATER,
    )
    assert runs.history(REPO, 7).prior == FINDINGS


def test_a_purged_pull_request_has_no_history(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.purge_content(REPO, 7, now=LATER)
    history = runs.history(REPO, 7)
    assert history.prior == ()
    assert history.high_water == 0


def test_another_pull_requests_history_is_not_borrowed(runs):
    runs.record(trigger(pr=8, key="k8"), head_sha=HEAD, result=result(), now=NOON)
    assert runs.history(REPO, 7).prior == ()


def test_the_first_completed_run_is_round_one(runs):
    runs.record(trigger(key="k1"), head_sha=HEAD, result=result(), now=NOON)
    assert runs.round_of(REPO, 7, "k1") == 1


def test_each_completed_run_is_the_next_round(runs):
    runs.record(trigger(key="k1"), head_sha=HEAD, result=result(), now=NOON)
    runs.record(trigger(key="k2"), head_sha=HEAD, result=result(), now=LATER)
    assert runs.round_of(REPO, 7, "k1") == 1
    assert runs.round_of(REPO, 7, "k2") == 2


def test_a_truncated_run_is_not_a_round(runs):
    """A round is a review that produced a comment, not an attempt."""
    runs.record(
        trigger(key="k1"),
        head_sha=HEAD,
        result=result((), outcome=Outcome.TRUNCATED),
        now=NOON,
    )
    runs.record(trigger(key="k2"), head_sha=HEAD, result=result(), now=LATER)
    assert runs.round_of(REPO, 7, "k2") == 1


def test_an_unknown_run_reads_as_round_one(runs):
    assert runs.round_of(REPO, 7, "never-recorded") == 1
