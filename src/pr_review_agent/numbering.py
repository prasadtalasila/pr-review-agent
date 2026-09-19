"""Finding numbers that survive a re-review.

The report numbers its items and keeps those numbers for the life of the pull
request, so a maintainer can write "item 9 is still open" and be understood.
That is only worth anything if a number means the same thing in round 4 that
it meant in round 2, which is what this module is for.

**Gaps are the feature.** A finding that gets fixed takes its number out of
circulation; the next round renders 1, 3, 5 and the missing 2 and 4 say, with
no words at all, that two earlier items were dealt with. Closing the gaps by
renumbering would throw that away and silently relabel everything a reader
had already referred to.

**A carried number is a claim, not a fact.** It arrives in engine output over
an untrusted tree, so it is checked against what this pull request has
actually issued: a number above the high-water mark was never handed out, and
a number two findings both claim cannot belong to both. Either way the
finding is treated as new rather than refused -- a wrong number is not a
reason to drop a real finding on the floor.
"""

from __future__ import annotations

from dataclasses import replace

from .engine import Finding


def assign(findings: tuple[Finding, ...], high_water: int) -> tuple[Finding, ...]:
    """Number ``findings``, honouring the numbers this pull request has issued.

    ``high_water`` is the largest number ever given out on this pull request;
    ``0`` on a first round. Input order is preserved, and a number is kept
    only if it is in ``1..high_water`` and no earlier finding already claimed
    it. Everything else is assigned from ``high_water + 1`` upwards.
    """
    claimed: set[int] = set()
    kept: list[int | None] = []
    for finding in findings:
        number = finding.number
        if number is not None and 1 <= number <= high_water and number not in claimed:
            claimed.add(number)
            kept.append(number)
        else:
            kept.append(None)
    nxt = high_water + 1
    numbered: list[Finding] = []
    for finding, number in zip(findings, kept):
        if number is None:
            number, nxt = nxt, nxt + 1
        numbered.append(replace(finding, number=number))
    return tuple(numbered)
