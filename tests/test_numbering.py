"""Stable finding numbers across review rounds."""

from pr_review_agent.engine import Finding, Severity
from pr_review_agent.numbering import assign


def finding(path="src/a.py", line=1, number=None, severity=Severity.MAJOR):
    return Finding(
        path=path,
        line=line,
        severity=severity,
        title="t",
        body="b",
        number=number,
    )


def numbers(findings):
    return [f.number for f in findings]


def test_a_first_round_numbers_from_one():
    assigned = assign((finding(line=1), finding(line=2)), high_water=0)
    assert numbers(assigned) == [1, 2]


def test_a_carried_number_is_kept():
    assigned = assign((finding(number=2),), high_water=4)
    assert numbers(assigned) == [2]


def test_a_new_finding_starts_after_the_high_water_mark():
    assigned = assign((finding(number=2), finding(line=9)), high_water=7)
    assert numbers(assigned) == [2, 8]


def test_a_gap_left_by_a_fixed_finding_is_never_closed():
    """Items 1 and 3 persist, 2 was fixed. 2 stays vacant; the new one is 5."""
    assigned = assign(
        (finding(number=1), finding(number=3), finding(line=9)), high_water=4
    )
    assert numbers(assigned) == [1, 3, 5]


def test_a_number_that_was_never_issued_is_refused():
    """Engine output is untrusted: it cannot invent a number above the mark."""
    assigned = assign((finding(number=99),), high_water=3)
    assert numbers(assigned) == [4]


def test_a_number_claimed_twice_is_kept_by_the_first_only():
    assigned = assign(
        (finding(line=1, number=2), finding(line=2, number=2)), high_water=5
    )
    assert numbers(assigned) == [2, 6]


def test_a_non_positive_number_is_refused():
    assigned = assign((finding(number=0),), high_water=3)
    assert numbers(assigned) == [4]


def test_numbering_is_deterministic_for_the_same_input():
    findings = (finding(line=5), finding(line=2), finding(number=1))
    first = numbers(assign(findings, high_water=2))
    assert first == numbers(assign(findings, high_water=2))


def test_no_findings_assigns_nothing():
    assert assign((), high_water=9) == ()
