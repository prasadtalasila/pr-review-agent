"""Allowlist: eligibility is keyed on user id, never on login."""

import pytest

from pr_review_agent.triggers.allowlist import Allowlist, AllowlistConfigError
from pr_review_agent.triggers.models import Actor


def test_matches_on_id_regardless_of_login():
    allowlist = Allowlist.from_config([1234])
    # Same account, renamed since the allowlist was written.
    assert allowlist.allows(Actor(user_id=1234, login="renamed-user"))


def test_rejects_impostor_who_took_the_freed_login():
    allowlist = Allowlist.from_config([1234])
    original = Actor(user_id=1234, login="alice")
    impostor = Actor(user_id=9999, login="alice")
    assert allowlist.allows(original)
    assert not allowlist.allows(impostor)


def test_numeric_strings_from_yaml_are_accepted():
    assert Allowlist.from_config(["1234"]).allows(Actor(1234, "alice"))


@pytest.mark.parametrize("entries", [["alice"], ["alice", 1234], [None], [True], [1.5]])
def test_non_numeric_entries_fail_loudly(entries):
    # A login-keyed allowlist would never match and silently disable triggers.
    with pytest.raises(AllowlistConfigError):
        Allowlist.from_config(entries)


def test_empty_allowlist_allows_nobody():
    assert not Allowlist.from_config([]).allows(Actor(1234, "alice"))


def test_actor_from_api_detects_bots():
    assert Actor.from_api({"id": 1, "login": "dependabot[bot]", "type": "Bot"}).is_bot
    assert Actor.from_api({"id": 2, "login": "renovate[bot]", "type": "User"}).is_bot
    assert not Actor.from_api({"id": 3, "login": "alice", "type": "User"}).is_bot


def test_actor_from_api_coerces_string_id():
    assert Actor.from_api({"id": "42", "login": "alice", "type": "User"}).user_id == 42
