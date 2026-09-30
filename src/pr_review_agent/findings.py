"""The one thing every layer of the agent handles: a finding.

Its own module, and a deliberately poor one -- ``dataclasses``, an enum
shim, and nothing else. Four layers need these two names: the engine
produces them, ``numbering`` stamps them, ``report`` lays them out and
``runs`` stores them. They lived in ``engine/models.py``, which is the
right home for what an *engine* is handed, and the wrong one for the type
that outlives every engine: importing ``Finding`` from there dragged in
``budget``, ``workspace`` and ``triggers`` with it.

That mattered once ``skill install`` started copying a renderer onto
machines with no ``pr_review_agent`` on them. Six modules with no
dependency beyond the standard library can travel; ``engine/models.py``
and its transitive closure -- httpx, sqlite, the config schema -- cannot.
Keep this module poor, and keep :mod:`pr_review_agent.report`,
:mod:`pr_review_agent.numbering`, :mod:`pr_review_agent.sanitise` and
:mod:`pr_review_agent.triggers.mention` poor with it. ``test_skill.py``
renders the worked example with the real package unimportable, which is
what fails when something rich gets added to any of them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._compat import StrEnum


class Severity(StrEnum):
    """How serious a finding claims to be.

    Advisory only. The publisher posts event ``COMMENT`` whatever a review
    concludes, so no severity -- not even ``BLOCKER`` -- can block or
    authorise a merge. See ``docs/DESIGN.md`` on prompt injection: a diff
    that talks its way to a high severity still cannot act.
    """

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


@dataclass(frozen=True)
class Finding:
    """One line-anchored remark, in the shape a review comment needs.

    ``title`` is the one-sentence headline the report renders in bold, and it
    states the consequence rather than the mechanism -- it is read first and
    often instead of the body. The remedy is the last paragraph of ``body``
    rather than a field of its own: ``FINDINGS_SCHEMA`` is kept small on
    purpose, because every required field is another way for a run to end in
    a validation failure that spent tokens and produced nothing.

    ``number`` is this finding's identity across review rounds, and it is the
    one field the engine may leave unset. A finding carried over from an
    earlier round keeps the number it was given; a new one is assigned the
    next free number by ``numbering.assign`` before it is recorded. Numbers
    are never reused and gaps are never closed, because a gap is what says an
    earlier item was fixed.
    """

    path: str
    line: int
    severity: Severity
    title: str
    body: str
    number: int | None = None


class Risk(StrEnum):
    """How much a mistake in this change could cost if it merged."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Recommendation(StrEnum):
    """The one verdict a report may carry, and it is advisory.

    Like ``Severity`` it cannot act: the publisher still posts a plain
    comment, never a review event, and writes no label. It is here so a
    maintainer triaging several pull requests can sort them, not so that
    anything downstream can.
    """

    SAFE_TO_MERGE = "safe_to_merge"
    MERGE_WITH_CAUTION = "merge_with_caution"
    CHANGES_REQUIRED = "changes_required"


#: How many paths ``priority_files`` may name. A list of every file is not a
#: priority, and the line it renders on has to stay one line.
MAX_PRIORITY_FILES = 5


@dataclass(frozen=True)
class Assessment:
    """The reviewer's view of the whole pull request, beside its findings.

    Required of every completed review (issue #126). Every value is the
    engine's judgement, not a measurement: ``effort`` is how long a
    maintainer needs to review the change, 1 to 5, and ``priority_files`` are
    the paths to read first. The bounds are checked here as well as in the
    schema, because a report rendered by hand never meets the schema.
    """

    effort: int
    risk: Risk
    recommendation: Recommendation
    priority_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.effort <= 5:
            raise ValueError(f"effort must be 1 to 5, got {self.effort}")
        if len(self.priority_files) > MAX_PRIORITY_FILES:
            raise ValueError(
                f"at most {MAX_PRIORITY_FILES} priority files, "
                f"got {len(self.priority_files)}"
            )
