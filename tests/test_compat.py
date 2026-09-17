"""The 3.10 StrEnum shim must behave exactly like the 3.11 built-in.

These assertions are the reason the CI matrix runs 3.10: the shim is only
imported there, so nothing else in the suite would notice it diverging.
"""

from pr_review_agent._compat import StrEnum
from pr_review_agent.poller.endpoints import Endpoint
from pr_review_agent.triggers.models import TriggerKind


def test_members_are_strings():
    assert isinstance(Endpoint.OPEN_PULLS, str)
    assert Endpoint.OPEN_PULLS == "open_pulls"


def test_str_returns_the_value_not_the_qualified_name():
    # The 3.10 `(str, Enum)` mixin returns "Endpoint.OPEN_PULLS" here, which
    # would silently change any interpolated log line or dict key.
    assert str(Endpoint.OPEN_PULLS) == "open_pulls"
    assert str(TriggerKind.PR_OPENED) == "pr_opened"


def test_format_and_interpolation_return_the_value():
    assert f"{TriggerKind.MENTION}" == "mention"
    assert format(TriggerKind.MENTION) == "mention"
    assert "{}".format(TriggerKind.MENTION) == "mention"  # noqa: UP032


def test_usable_as_a_mapping_key():
    assert {Endpoint.ISSUE_COMMENTS: 1}[Endpoint.ISSUE_COMMENTS] == 1


def test_subclassing_still_yields_a_str_enum():
    class Colour(StrEnum):
        RED = "red"

    assert str(Colour.RED) == "red"
    assert Colour.RED == "red"
