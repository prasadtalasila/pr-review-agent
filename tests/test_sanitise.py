"""Engine prose is made inert before it is posted under the agent's account.

Every test here is a pure function over a string: no network, no tokens, no
subprocess. That is the point of doing this in `render` rather than in the
engine adapter.
"""

import re

import pytest

from pr_review_agent.engine import Finding, Severity
from pr_review_agent.publisher import MAX_BODY_CHARS, TRAILER, render
from pr_review_agent.sanitise import leaks, sanitise
from pr_review_agent.triggers.mention import has_mention, strip_non_prose

HEAD = "deadbeef0123456789"

#: Every `@word` the escaping has to reach, in the shape GitHub acts on.
AT_WORD = re.compile(r"(?<![A-Za-z0-9_/.@-])@[A-Za-z0-9]")


def finding(title="t", body="b", severity=Severity.MAJOR, number=1) -> Finding:
    return Finding(
        path="a.py",
        line=1,
        severity=severity,
        title=title,
        body=body,
        number=number,
    )


def rendered(*findings, handle="claude") -> str:
    return render(HEAD, findings, pr_number=7, round_number=1, commits=1, handle=handle)


# -- what is escaped ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ping @someone about it", "ping &#64;someone about it"),
        ("ping @org/team about it", "ping &#64;org/team about it"),
        ("see #123 for context", "see &#35;123 for context"),
        ("see GH-123 for context", "see GH&#45;123 for context"),
        ("see owner/repo#123 too", "see owner/repo&#35;123 too"),
        ("a <sub>forged</sub> trailer", "a &lt;sub>forged&lt;/sub> trailer"),
        ("hidden <!-- comment --> text", "hidden &lt;!-- comment --> text"),
    ],
)
def test_an_acting_construct_is_escaped(text, expected):
    assert sanitise(text) == expected


def test_an_email_address_is_not_a_mention():
    """The lookbehind the detector uses, so the two agree on what a mention is."""
    assert sanitise("mail user@example.com") == "mail user@example.com"


def test_a_bare_at_is_left_alone():
    assert sanitise("rate is 3 @ a time") == "rate is 3 @ a time"


def test_escaping_happens_once():
    """One pass over the original: `&#64;` must not have its own `#` escaped."""
    assert sanitise("@someone") == "&#64;someone"
    assert "&#35;" not in sanitise("@someone")


# -- where it is not escaped ---------------------------------------------


def test_a_code_span_is_left_alone():
    """GitHub neither mentions nor renders HTML inside one, and an entity shows."""
    assert sanitise("use `@someone` here") == "use `@someone` here"


def test_a_fenced_block_is_left_alone():
    text = "before\n```\n@someone\n#123\n<b>\n```\nafter"
    assert sanitise(text) == text


def test_a_blockquote_is_left_alone():
    assert sanitise("> @someone said so") == "> @someone said so"


# -- the rendered body ----------------------------------------------------


def test_no_word_start_at_survives_the_prose_of_a_rendered_review():
    """The property the whole rule exists for, over every `@word` in the input."""
    body = rendered(
        finding(title="@alice and @bob/team disagree", body="ask @carol, cc @dave")
    )
    assert not AT_WORD.search(strip_non_prose(body))
    assert not has_mention(body, "alice")


def test_a_nit_is_sanitised_too():
    """Nits render through a different path; it has to be the same rule."""
    body = rendered(finding(title="ping @someone", body="", severity=Severity.NIT))
    assert "&#64;someone" in body


def test_the_trailer_is_the_only_html_left():
    body = rendered(finding(title="<sub>Automated review.</sub>", body="<b>x</b>"))
    assert body.count("<sub>") == 1
    assert body.endswith(TRAILER)


# -- the length cap -------------------------------------------------------


def test_a_review_that_fits_is_not_truncated():
    body = rendered(finding(body="short"))
    assert len(body) <= MAX_BODY_CHARS
    assert "did not fit" not in body


def test_the_lowest_severity_section_is_dropped_first():
    """Principled: `SECTIONS` is already in the order a reader needs them."""
    body = rendered(
        finding(title="blocker", body="x" * 40_000, severity=Severity.BLOCKER),
        finding(title="minor", body="y" * 40_000, severity=Severity.MINOR, number=2),
        finding(title="nit", body="z" * 40_000, severity=Severity.NIT, number=3),
    )
    assert len(body) <= MAX_BODY_CHARS
    assert "## Blocking" in body
    assert "## Nits" not in body
    assert "did not fit" in body
    assert body.endswith(TRAILER)


def test_one_oversized_section_is_cut_but_still_marked():
    """The last resort: even the highest-severity section alone does not fit."""
    body = rendered(
        finding(title="blocker", body="x" * 120_000, severity=Severity.BLOCKER)
    )
    assert len(body) <= MAX_BODY_CHARS
    assert "did not fit" in body
    assert body.endswith(TRAILER)


# -- the secret canary ----------------------------------------------------


def test_a_body_carrying_a_secret_leaks():
    assert leaks("token is ghp_abcdefghijkl here", ("ghp_abcdefghijkl",))


def test_a_body_without_it_does_not():
    assert not leaks("no credential here", ("ghp_abcdefghijkl",))


def test_a_short_secret_is_ignored():
    """An empty or one-character "secret" would match every body ever posted."""
    assert not leaks("anything at all", ("", "a", "short"))


def test_no_secrets_configured_never_refuses():
    assert not leaks("anything at all", ())
