"""How a review is laid out, with nothing about how it is posted.

Split out of :mod:`pr_review_agent.publisher`, which keeps the half that
talks to GitHub: reactions, comment ids, the run store, the live head read.
None of that is needed to turn findings into text, and the split is what
lets this module stay poor -- ``dataclasses``, ``re`` and two sibling
modules that are equally poor.

Poor on purpose. ``skill install`` copies this file, :mod:`.findings`,
:mod:`.numbering`, :mod:`.sanitise`, :mod:`.triggers.mention` and
``_compat`` into the skill directory, so a review written by hand renders
through *this* code on a machine that has no ``pr_review_agent`` installed
at all. The alternative was a second renderer in the skill, and two
renderers is the drift the whole arrangement exists to prevent. Add an
import here that reaches config, sqlite or httpx and the copy stops
working; ``test_skill.py`` renders the worked example with the real
package blocked, which is what notices.

The contract this implements is ``docs/reporting/review-report.md``.
"""

from __future__ import annotations

from .findings import Finding, Severity
from .sanitise import sanitise
from .triggers.mention import neutralise

#: Which heading each severity renders under, in the order a reader sees
#: them. Pinned here, where a test can read it, because iteration order over
#: an enum is a definition detail rather than a promise -- and because a
#: reader comparing this round's comment with the last one is reading a
#: diff, and only a pinned order makes the things that changed the things
#: that stand out.
#:
#: ``major`` and ``minor`` share a heading on purpose. ``Severity`` is
#: persisted and asserted across the suite, so it is not collapsed to three
#: values; but a ``major`` finding that is not a blocker must not be printed
#: under a heading claiming it blocks.
SECTIONS: tuple[tuple[str, tuple[Severity, ...]], ...] = (
    ("Blocking", (Severity.BLOCKER,)),
    ("Should fix", (Severity.MAJOR, Severity.MINOR)),
    ("Nits", (Severity.NIT,)),
)

#: Severity to its rank, derived from ``SECTIONS`` so the two cannot drift.
_RANK: dict[Severity, int] = {
    severity: index
    for index, (_, severities) in enumerate(SECTIONS)
    for severity in severities
}

#: Said once, on every comment. An automated remark that reads like a
#: verdict invites being treated as one, and this agent's opinion is
#: deliberately not one.
TRAILER = (
    "<sub>Automated review. It takes no action on this pull request "
    "beyond this comment.</sub>"
)

#: Said at the end of every refusal notice. Both halves are load-bearing:
#: a contributor who has just been refused wants to know whether it cost
#: anything, and what to do next -- and the answer to the second is never
#: "wait", because a deterministic refusal ends the trigger for good.
REFUSAL_TRAILER = (
    "<sub>No review ran and nothing was charged. Once the pull request or "
    "the configuration has changed, a new @{handle} comment asks again.</sub>"
)

#: Under a failure notice. Unlike a refusal, the review did run -- and may
#: have been charged -- so it says only what is true of every such failure.
FAILURE_TRAILER = (
    "<sub>The operator's log has the details. Once they are fixed, a new "
    "@{handle} comment asks again.</sub>"
)

#: How long a rendered body may be. GitHub rejects a comment over 65 536
#: characters with a 422 -- *after* the review was paid for -- and the
#: worker would then re-offer the same body on every claim forever. The
#: margin absorbs the header, the trailer and the entities escaping adds.
MAX_BODY_CHARS = 60_000

#: Said in place of the sections that did not fit, so a reader is never
#: shown a partial review that looks complete.
TRUNCATION_NOTE = (
    "_Some findings were omitted: this review did not fit a GitHub comment. "
    "The lowest-severity sections were dropped first._"
)


def render(
    head_sha: str,
    findings: tuple[Finding, ...],
    *,
    pr_number: int,
    round_number: int,
    commits: int,
    handle: str,
    moved_to: str | None = None,
) -> str:
    """The comment body for a review of ``head_sha``.

    The commit is named because a pull request under review has several,
    and a reader scrolling past two comments from the agent has to be able
    to tell which revision each of them describes. The round and the commit
    count are there for the same reason -- "round 3" and "round 1" are
    different statements, including when both found nothing.

    A finding renders no ``path:line`` anchor. The paths that matter are the
    ones the reviewer names in its own prose, which is what the reference
    report this template was drawn from does; an anchor beside every headline
    reads as machine output and crowds the sentence meant to be read first.
    The location is still on the stored ``Finding``, where a future
    line-anchored comment would need it.

    ``handle`` is required rather than defaulted. ``"claude"`` is already
    spelled as the default of ``TriggerConfig.handle``, and a second copy
    here is a copy that can drift -- in the one direction where drift means
    the agent summons itself.

    Finding titles and bodies are engine output over an untrusted tree, so
    every one of them goes through ``sanitise`` before it is assembled:
    what a reader sees is unchanged, and what GitHub would have *done* with
    it -- notify an account, cross-reference an issue, render HTML -- it no
    longer does. The trailer is the only HTML in the result, and it is ours.
    See ``docs/reporting/review-report.md`` for the contract this
    implements.
    """
    header = (
        f"## Review: PR #{pr_number} — round {round_number} "
        f"(`{head_sha[:7]}`, {commits} commits)"
    )
    if moved_to is not None:
        header = f"{header}\n\n{_moved_note(head_sha, moved_to)}"
    if not findings:
        return neutralise(f"{header}\n\nNo issues found.\n\n{TRAILER}", handle)
    ordered = sorted(findings, key=_order)
    sections = []
    for heading, severities in SECTIONS:
        section = [f for f in ordered if f.severity in severities]
        if not section:
            continue
        rendered = _prose(section) if heading == "Nits" else _items(section)
        sections.append(f"## {heading}\n\n{rendered}")
    return neutralise(_fit(header, sections), handle)


def refusal(notice: str, *, handle: str) -> str:
    """Assemble one deterministic refusal into a comment body.

    Pure, and short enough that the length cap :func:`render` works around
    cannot be reached: every notice is fixed text over a handful of
    integers and configuration key names this process chose itself.

    It still goes through ``neutralise``, which is not ceremony. The
    trailer tells the reader to comment ``@handle`` again, and a comment
    the agent posts is a comment the poller reads back -- so without it the
    advice would summon the review it is explaining the absence of, on a
    pull request already known to be unreviewable. What ``sanitise`` adds
    for a review is left out, because there is no engine prose here for it
    to escape.
    """
    return neutralise(
        f"**Not reviewed.** {notice}\n\n{REFUSAL_TRAILER.format(handle=handle)}",
        handle,
    )


def failure(notice: str, *, handle: str, attempts: int) -> str:
    """Assemble the notice for a review whose every attempt failed.

    Fixed text over integers, like :func:`refusal`, and neutralised for the
    same reason: the trailer's ``@handle`` must not summon a review.
    """
    return neutralise(
        f"**Not reviewed.** {notice} That was the last of {attempts} attempts, "
        "so the agent has stopped trying.\n\n" + FAILURE_TRAILER.format(handle=handle),
        handle,
    )


def _moved_note(head_sha: str, moved_to: str) -> str:
    """Said above a review whose commit is no longer the head.

    The tokens were spent before the head was re-read, so the choice is
    between a review nobody sees and one that says what it describes. Most
    of a review survives a fixup commit, and the note is what keeps a reader
    who finds this comment later from taking it for a review of the head.
    """
    return (
        f"_This review describes `{head_sha[:7]}`, which is no longer the head: "
        f"the branch has since moved to `{moved_to[:7]}`. Findings may already "
        "be addressed. Mention me again for a review of the new head._"
    )


def _fit(header: str, sections: list[str]) -> str:
    """The body, trimmed to ``MAX_BODY_CHARS`` if it does not fit.

    Whole sections go first, lowest severity first, because ``SECTIONS`` is
    already in descending order of how much the reader needs them -- so the
    cut is principled rather than wherever the character count landed. Only
    when the *highest*-severity section alone is over the limit is prose cut
    mid-sentence, and it is still marked.
    """
    kept = list(sections)
    while True:
        body = _assemble(header, kept, truncated=len(kept) < len(sections))
        if len(body) <= MAX_BODY_CHARS:
            return body
        if len(kept) == 1:
            break
        kept.pop()
    room = MAX_BODY_CHARS - len(_assemble(header, [""], truncated=True))
    return _assemble(header, [kept[0][: max(room, 0)].rstrip()], truncated=True)


def _assemble(header: str, sections: list[str], *, truncated: bool) -> str:
    """Header, sections, the truncation note if one is owed, then the trailer."""
    parts = [header, *sections]
    if truncated:
        parts.append(TRUNCATION_NOTE)
    parts.append(TRAILER)
    return "\n\n".join(parts)


def _items(findings: list[Finding]) -> str:
    """Numbered entries: bold headline, then the body indented beneath it."""
    return "\n\n".join(
        f"{finding.number}. **{sanitise(finding.title)}**\n\n"
        f"{_indent(sanitise(finding.body))}"
        for finding in findings
    )


def _prose(findings: list[Finding]) -> str:
    """Nits, run together as sentences. One that needs an entry is not a nit."""
    return " ".join(
        sanitise(f"{finding.title} {finding.body}".strip()) for finding in findings
    )


def _indent(body: str) -> str:
    """Indent a body under its numbered entry, leaving blank lines blank."""
    return "\n".join(f"   {line}" if line.strip() else "" for line in body.splitlines())


def _order(finding: Finding) -> tuple[int, int, str, int]:
    """Section, then number, then location -- so the same findings render the same.

    ``number`` sorts before location so a report's entries ascend, and a
    finding is numbered before it is recorded, so ``None`` never reaches here
    on a published run. It is tolerated rather than asserted because a header
    is not worth failing a publish over.
    """
    return (
        _RANK[finding.severity],
        finding.number if finding.number is not None else 0,
        finding.path,
        finding.line,
    )
