"""What a review engine is given, and what it must hand back.

This is the one seam the design turns on. Polling, allowlisting, dedupe,
leasing, the window arithmetic and publishing are all agent-agnostic; only
"run a review" is specific to a particular coding agent, so only that step
is swappable. See ``docs/ENGINE.md``.

Nothing here spends anything or claims a queue row. The types are the
contract; the adapters that satisfy it land separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .._compat import StrEnum
from ..budget import Mode, Usage, UsageConfidence
from ..triggers.models import Trigger
from ..workspace import Checkout, PullRequestFacts


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

    Deliberately four fields. The publisher does not exist yet, and the
    rationale, code excerpt and retention machinery ``docs/DESIGN.md``
    sketches would be designing its data model before it has one.
    """

    path: str
    line: int
    severity: Severity
    body: str


@dataclass(frozen=True)
class Capabilities:
    """What an engine can do, declared rather than discovered.

    Every field is required: an adapter has to state its answer, because a
    default would be a claim nobody made. ``usage_reporting`` is the field
    that matters most -- an engine that reports no tokens forces the
    governor onto proxy controls (run count, wall clock, turn caps), which
    is a materially weaker guarantee than a token count, and the worker has
    to know that in advance rather than discover it after a run.

    ``read_only_sandbox``, ``subagents`` and ``prompt_caching`` have no
    consumer yet. They are named here because ``docs/DESIGN.md`` names them
    and because an adapter's answer is knowable when the adapter is written,
    not later.
    """

    structured_output: bool
    usage_reporting: bool
    read_only_sandbox: bool
    subagents: bool
    prompt_caching: bool


@dataclass(frozen=True)
class ReviewRequest:
    """Everything an engine gets, and nothing it does not.

    The diff is not repeated alongside ``checkout``: it is already on the
    checkout, and two copies could disagree. Both the tree and the diff are
    untrusted input -- an engine may read them, and must not let them widen
    what it is allowed to do.

    ``mode`` is the rung of the degradation ladder the run was admitted
    under, which is what lets an engine spend less when the budget is tight
    rather than refuse outright.
    """

    checkout: Checkout
    facts: PullRequestFacts
    trigger: Trigger
    mode: Mode


@dataclass(frozen=True)
class ReviewResult:
    """What a finished review produced, and what it cost.

    ``usage`` is ``budget.Usage`` itself rather than a parallel type, so
    "carries everything ``Governor.settle`` needs" holds by construction:
    ``settle`` takes exactly this object.
    """

    findings: tuple[Finding, ...]
    usage: Usage

    def __post_init__(self) -> None:
        # An unknown cost and a number are mutually exclusive answers. A
        # settled ledger row claiming both would let a run that reported
        # nothing still draw down a window, which is the one direction the
        # governor cannot detect afterwards.
        if self.usage.confidence is UsageConfidence.UNAVAILABLE and self.usage.tokens:
            raise ValueError(
                "usage_confidence 'unavailable' cannot report "
                f"{self.usage.tokens} tokens"
            )
        if not self.usage.engine:
            raise ValueError("ReviewResult.usage must name the engine that ran")


@runtime_checkable
class ReviewEngine(Protocol):
    """Run one review. The only agent-specific step in the system."""

    #: Recorded on the ledger row, so a posted comment is traceable to what
    #: produced it.
    name: str

    @property
    def capabilities(self) -> Capabilities:
        """What this engine can do."""
        raise NotImplementedError

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Review the checkout described by ``request``."""
        raise NotImplementedError
