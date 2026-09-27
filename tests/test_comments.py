"""AgentComments: which comments the agent posted, so it cannot answer them."""

from datetime import datetime, timedelta, timezone

import pytest

from pr_review_agent.comments import AgentComments
from pr_review_agent.store import SqliteStore

NOON = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
LATER = NOON + timedelta(minutes=5)
REPO = "o/r"


@pytest.fixture(name="posted")
def posted_fixture(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        yield AgentComments(store)


def test_a_recorded_id_comes_back(posted):
    posted.record(REPO, 555, now=NOON)
    assert posted.ids_for(REPO) == frozenset({555})


def test_nothing_posted_is_an_empty_set(posted):
    assert posted.ids_for(REPO) == frozenset()


def test_recording_the_same_id_twice_is_not_an_error(posted):
    """The publisher is retried; a second write of the same fact is the fact."""
    posted.record(REPO, 555, now=NOON)
    posted.record(REPO, 555, now=LATER)
    assert posted.ids_for(REPO) == frozenset({555})


def test_another_repositorys_comments_are_not_mine(posted):
    """One store can back several repositories, and ids are per repository."""
    posted.record("o/other", 555, now=NOON)
    assert posted.ids_for(REPO) == frozenset()


def test_every_id_is_kept(posted):
    """One comment per review, so the set grows rather than being replaced."""
    for comment_id in (555, 556, 557):
        posted.record(REPO, comment_id, now=NOON)
    assert posted.ids_for(REPO) == frozenset({555, 556, 557})


def test_a_naive_timestamp_is_refused(posted):
    with pytest.raises(ValueError, match="timezone-aware"):
        posted.record(REPO, 555, now=datetime(2026, 9, 27, 12, 0))


def test_the_record_survives_a_reopen(tmp_path):
    """It is the agent's memory of what it said, so it outlives the process."""
    path = tmp_path / "state.db"
    with SqliteStore(path) as store:
        AgentComments(store).record(REPO, 555, now=NOON)
    with SqliteStore(path) as reopened:
        assert AgentComments(reopened).ids_for(REPO) == frozenset({555})
