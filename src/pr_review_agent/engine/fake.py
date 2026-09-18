"""An engine that satisfies the protocol and spends nothing.

The seam pays for itself here: the review step becomes testable without a
network, an API key or a token. Every test in the suite that needs an engine
uses this one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..budget import Usage, UsageConfidence
from .models import Capabilities, Finding, ReviewRequest, ReviewResult

#: What a well-behaved engine looks like: reports its tokens, emits
#: structured output, runs read-only. An adapter that cannot do one of these
#: is the interesting case, so tests that care pass their own record.
FULL = Capabilities(
    structured_output=True,
    usage_reporting=True,
    read_only_sandbox=True,
    subagents=True,
    prompt_caching=True,
)


@dataclass
class FakeEngine:
    """Return canned findings, record what was asked, cost nothing.

    ``usage`` must agree with ``capabilities.usage_reporting``: an engine
    that reports no tokens has to settle as ``unavailable``. Nothing here
    enforces that -- the conformance tests do, because that is exactly the
    rule a real adapter has to be held to.
    """

    findings: tuple[Finding, ...] = ()
    usage: Usage = Usage(
        tokens=1_000,
        confidence=UsageConfidence.EXACT,
        engine="fake",
        model="fake-1",
    )
    capabilities: Capabilities = FULL
    name: str = "fake"
    #: Every request handed to this engine, in order, so a test can assert
    #: what the worker passed rather than that it passed something.
    requests: list[ReviewRequest] = field(default_factory=list)

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Record the request and return the canned result."""
        self.requests.append(request)
        return ReviewResult(findings=self.findings, usage=self.usage)
