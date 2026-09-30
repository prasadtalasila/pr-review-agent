"""RunStore: what a paid review produced, so publishing can be retried."""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.budget import Usage, UsageConfidence
from pr_review_agent.engine import Finding, Outcome, ReviewResult, Severity
from pr_review_agent.runs import RunStore, publication_of, published_run_key
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


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield store


@pytest.fixture(name="runs")
def runs_fixture(store):
    return RunStore(store)


def row(store, key, column):
    """One column of one run, for the state no reader returns."""
    with store.transaction() as conn:
        return conn.execute(
            f"SELECT {column} FROM runs WHERE dedupe_key = :key", {"key": key}
        ).fetchone()[0]


def test_a_recorded_run_round_trips(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    recorded = runs.unpublished("pr_opened:o/r:7:deadbeef")
    assert recorded.dedupe_key == "pr_opened:o/r:7:deadbeef"
    assert recorded.head_sha == HEAD
    assert recorded.outcome is Outcome.COMPLETED
    assert recorded.findings == FINDINGS


def test_a_clean_run_records_no_findings(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(findings=()), now=NOON)
    assert runs.unpublished("pr_opened:o/r:7:deadbeef").findings == ()


def test_recording_the_same_run_twice_refreshes_it(runs):
    """A resumed claim re-records rather than raising."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.record(trigger(), head_sha="newer", result=result(findings=()), now=LATER)
    recorded = runs.unpublished("pr_opened:o/r:7:deadbeef")
    assert recorded.head_sha == "newer"
    assert recorded.findings == ()


def test_a_published_run_is_not_offered_again(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published(
        "pr_opened:o/r:7:deadbeef", comment_id=555, now=LATER, outcome="published"
    )
    assert runs.unpublished("pr_opened:o/r:7:deadbeef") is None


def test_a_run_that_was_never_recorded_is_not_offered(runs):
    runs.record(trigger(pr=8, key="k8"), head_sha=HEAD, result=result(), now=NOON)
    assert runs.unpublished("pr_opened:o/r:7:deadbeef") is None


def test_each_unpublished_run_is_offered_under_its_own_key(runs):
    """A publication item names one run, not "the oldest here"."""
    runs.record(trigger(key="first"), head_sha=HEAD, result=result(), now=NOON)
    runs.record(trigger(key="second"), head_sha=HEAD, result=result(), now=LATER)
    assert runs.unpublished("first").dedupe_key == "first"
    assert runs.unpublished("second").dedupe_key == "second"


def test_marking_published_records_the_comment(runs, store):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published(
        "pr_opened:o/r:7:deadbeef", comment_id=555, now=LATER, outcome="published"
    )
    assert row(store, "pr_opened:o/r:7:deadbeef", "comment_id") == 555


def test_each_round_records_the_comment_it_posted(runs, store):
    """One comment per review: the second round does not overwrite the first."""
    runs.record(trigger(key="first"), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published("first", comment_id=555, now=NOON, outcome="published")
    runs.record(trigger(key="second"), head_sha=HEAD, result=result(), now=LATER)
    runs.mark_published("second", comment_id=556, now=LATER, outcome="published")
    assert row(store, "first", "comment_id") == 555
    assert row(store, "second", "comment_id") == 556


def test_purging_empties_the_content_and_keeps_the_rest(runs, store):
    """The ledger survives the purge, and so does the comment id."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published(
        "pr_opened:o/r:7:deadbeef", comment_id=555, now=NOON, outcome="published"
    )
    assert runs.purge_content(REPO, 7, now=LATER) == 1
    assert row(store, "pr_opened:o/r:7:deadbeef", "comment_id") == 555


def test_what_the_review_did_not_read_is_kept_for_a_retried_post(runs):
    """The checkout is gone by the time a failed post is retried."""
    withheld = (("web/node_modules/", 300), ("yarn.lock", 1))
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON, omitted=withheld)
    assert runs.unpublished("pr_opened:o/r:7:deadbeef").omitted == withheld


def test_purging_empties_what_the_review_did_not_read(runs, store):
    """The paths are the contributor's tree, like the findings."""
    withheld = (("yarn.lock", 1),)
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON, omitted=withheld)
    runs.purge_content(REPO, 7, now=LATER)
    assert row(store, "pr_opened:o/r:7:deadbeef", "omitted") == "[]"


def test_purging_twice_purges_nothing_the_second_time(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.purge_content(REPO, 7, now=LATER)
    assert runs.purge_content(REPO, 7, now=LATER) == 0


def test_a_purged_run_is_never_offered_for_publication(runs):
    """Its findings are gone, so there is nothing left to post."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.purge_content(REPO, 7, now=LATER)
    assert runs.unpublished("pr_opened:o/r:7:deadbeef") is None


# -- giving up on a post GitHub will never accept -------------------------


KEY = "pr_opened:o/r:7:deadbeef"


def test_a_failed_post_short_of_the_limit_is_not_given_up_on(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    assert runs.publish_failed(KEY, limit=3, now=LATER) is False
    assert runs.publish_failed(KEY, limit=3, now=LATER) is False
    assert runs.unpublished(KEY) is not None


def test_the_last_attempt_allowed_stamps_the_run(runs, store):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    for _ in range(2):
        runs.publish_failed(KEY, limit=3, now=LATER)
    assert runs.publish_failed(KEY, limit=3, now=LATER) is True
    assert row(store, KEY, "publish_attempts") == 3
    assert row(store, KEY, "publish_failed_at") is not None


def test_a_run_given_up_on_is_never_offered_for_publication(runs):
    """The whole point: the retry loop stops, and the ERROR stops with it."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.publish_failed(KEY, limit=1, now=LATER)
    assert runs.unpublished(KEY) is None


def test_clearing_the_stamp_offers_the_run_again(runs, store):
    """The operator's recovery, as docs/STORAGE.md describes it."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.publish_failed(KEY, limit=1, now=LATER)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE runs SET publish_failed_at = NULL, publish_attempts = 0 "
            "WHERE dedupe_key = :key",
            {"key": KEY},
        )
    assert runs.unpublished(KEY) is not None


def test_a_run_posted_in_the_meantime_is_not_stamped(runs, store):
    """`published_at` is set, so there is no failure left to record."""
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.mark_published(KEY, comment_id=555, now=NOON, outcome="published")
    assert runs.publish_failed(KEY, limit=1, now=LATER) is False
    assert row(store, KEY, "publish_attempts") == 0


def test_a_run_that_was_never_recorded_is_not_a_failure(runs):
    assert runs.publish_failed("never-recorded", limit=1, now=LATER) is False


def test_a_naive_timestamp_is_refused(runs):
    with pytest.raises(ValueError, match="timezone-aware"):
        runs.record(
            trigger(), head_sha=HEAD, result=result(), now=datetime(2026, 9, 18, 12)
        )


def test_a_publication_item_names_the_run_it_posts(runs):
    """The round trip the worker's `PUBLISH` item depends on."""
    recorded = runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    item = publication_of(trigger(), recorded)

    assert item.kind is TriggerKind.PUBLISH
    assert item.dedupe_key != recorded.dedupe_key
    assert item.repo == REPO and item.pr_number == 7
    assert item.actor_id == 99  # still that contributor's review
    assert published_run_key(item) == recorded.dedupe_key
    assert runs.unpublished(published_run_key(item)).dedupe_key == recorded.dedupe_key


def test_a_review_trigger_names_no_recorded_run(runs):
    """Reading a run key off a review row would be reading it off nothing."""
    del runs
    with pytest.raises(ValueError, match="not a publication item"):
        published_run_key(trigger())


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
    assert runs.unpublished("pr_opened:o/r:7:deadbeef").findings == numbered


def test_a_finding_stored_before_titles_existed_still_loads(runs):
    """A row written by an older build has no title and no number."""
    runs.record(trigger(), head_sha=HEAD, result=result(()), now=NOON)
    legacy = '[{"path": "src/a.py", "line": 12, "severity": "major", "body": "old"}]'
    with runs._store.transaction() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE runs SET findings = :f WHERE repo = :r AND pr_number = :p",
            {"f": legacy, "r": REPO, "p": 7},
        )
    assert runs.unpublished("pr_opened:o/r:7:deadbeef").findings == (
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


# -- what an incremental round diffs from --


def test_a_first_review_has_nothing_to_diff_from(runs):
    assert runs.history(REPO, 7).incremental_base(LATER, 0) is None


def test_the_newest_completed_rounds_head_is_the_one_to_diff_from(runs):
    runs.record(trigger(key="k1"), head_sha="a" * 40, result=result(), now=NOON)
    runs.record(trigger(key="k2"), head_sha="b" * 40, result=result(), now=LATER)
    runs.record(
        trigger(key="k3"),
        head_sha="c" * 40,
        result=result((), outcome=Outcome.TRUNCATED),
        now=LATER,
    )
    history = runs.history(REPO, 7)
    assert (history.head_sha, history.recorded_at) == ("b" * 40, LATER)
    assert history.incremental_base(LATER, 0) == "b" * 40


def test_a_round_newer_than_the_threshold_is_not_diffed_from(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    history = runs.history(REPO, 7)
    gap = int((LATER - NOON).total_seconds())
    assert history.incremental_base(LATER, gap + 1) is None
    assert history.incremental_base(LATER, gap) == HEAD


def test_a_purged_round_is_not_diffed_from(runs):
    runs.record(trigger(), head_sha=HEAD, result=result(), now=NOON)
    runs.purge_content(REPO, 7, now=LATER)
    assert runs.history(REPO, 7).incremental_base(LATER, 0) is None


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
