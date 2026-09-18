"""The ``claude`` CLI as a review engine: the first adapter that can spend.

Three flags carry the design, and all three are argv a test can assert rather
than prompt wording a model can be talked out of:

- ``--tools`` names the whole tool set, and it is read-only. No Write, no
  Edit, no Bash.
- ``--restricted`` removes the command-running tools a second time, and
  ignores user, project and local settings files -- so a ``CLAUDE.md`` in the
  tree under review is not instructions to the reviewer.
- ``--permission-prompts none`` means nothing can escalate by asking:
  anything that would prompt is denied outright.

``--bare`` would give the same isolation and was rejected: it forces
``ANTHROPIC_API_KEY`` authentication, which would silently settle the billing
question that is still open.
"""

from __future__ import annotations

import json
import logging

from ..budget import Usage, UsageConfidence
from .cli import CliEngine, EngineProtocolError, UsageLimited
from .models import (
    Capabilities,
    Finding,
    Outcome,
    ReviewRequest,
    ReviewResult,
    Severity,
)
from .prompt import FINDINGS_SCHEMA, SYSTEM_PROMPT, build_prompt
from .standards import read_standards

logger = logging.getLogger(__name__)

#: The whole tool set. Adding a write tool has to break a test.
TOOLS = "Read,Grep,Glob"

#: What the envelope's ``subtype`` means for publishability. Anything absent
#: from here is a failure: an unrecognised subtype is not a clean review.
_TRUNCATING_SUBTYPES = frozenset({"error_max_turns"})

#: How this adapter recognises the account being out of quota, as opposed to
#: this run being out of its own ceiling. Matched case-insensitively against
#: the envelope and against stderr, because it is not yet known which of the
#: two carries it.
#:
#: **These markers are a guess, and the one place to correct it.** Issue #20
#: requires the real failure to be observed before the interface is fixed;
#: it has not been, because manufacturing one means driving a live
#: subscription into the wall this whole design exists to avoid. When the
#: real error is seen, editing this tuple is the entire fix -- no signature
#: changes, no migration. Until then a miss costs a retry rather than a
#: wrong trip, which is the safe direction: the breaker refuses work, so a
#: false positive is more expensive than a false negative.
_USAGE_LIMIT_MARKERS = (
    "usage limit reached",
    "rate_limit_error",
    "exceeded your account's",
)

#: Five answers about this adapter *as it is configured*, which is the only
#: form in which they are true: ``read_only_sandbox`` is a claim about the
#: argv, so ``subagents`` has to be read the same way. The CLI can fan out;
#: the tool set above does not let it, so the honest answer is ``False``.
CAPABILITIES = Capabilities(
    # `--json-schema` validates the final output and re-prompts on mismatch.
    structured_output=True,
    usage_reporting=True,
    read_only_sandbox=True,
    subagents=False,
    prompt_caching=True,
)


class ClaudeCliEngine(CliEngine):
    """Review a checkout by running ``claude -p`` over it."""

    name = "claude"
    #: Where an API key lives, when one is used at all.
    env_prefixes = ("ANTHROPIC_",)

    def __init__(
        self,
        *,
        model: str,
        expected_version: str,
        binary: str = "claude",
        timeout_seconds: float = 900.0,
        standards_paths: tuple[str, ...] = (),
    ) -> None:
        super().__init__(binary=binary, timeout_seconds=timeout_seconds)
        self.model = model
        self.expected_version = expected_version
        self.standards_paths = standards_paths
        self._version_checked = False

    @property
    def capabilities(self) -> Capabilities:
        """What this engine can do."""
        return CAPABILITIES

    def argv(self, request: ReviewRequest) -> tuple[str, ...]:
        """The command line for one review. Pinned element by element."""
        del request  # The mode-aware argv belongs with the per-run ceilings.
        return (
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(FINDINGS_SCHEMA, sort_keys=True),
            "--model",
            self.model,
            "--system-prompt",
            SYSTEM_PROMPT,
            "--tools",
            TOOLS,
            "--restricted",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
        )

    async def prompt(self, request: ReviewRequest) -> str:
        """The review prompt, with the standards read at the merge base."""
        standards = await read_standards(request.checkout, self.standards_paths)
        return build_prompt(request, standards)

    async def preflight(self) -> None:
        """Warn on a version this adapter was not written against.

        A warning rather than a refusal: the parse is what actually protects
        us and it already fails loudly, while refusing would take the
        reviewer offline on a routine upgrade that changed nothing we read.
        """
        if self._version_checked:
            return
        self._version_checked = True
        found = await self.version()
        if not found.startswith(self.expected_version):
            logger.warning(
                "%s reports %r; this adapter was written against %r. "
                "Output parsing may fail.",
                self.binary,
                found,
                self.expected_version,
            )

    def usage_limited(self, text: str) -> bool:
        """Whether ``text`` is the CLI saying the *account* is out of quota."""
        lowered = text.lower()
        return any(marker in lowered for marker in _USAGE_LIMIT_MARKERS)

    def parse(self, stdout: str) -> ReviewResult:
        """Read the result envelope, strictly."""
        envelope = self._envelope(stdout)
        usage = self._usage(envelope)
        if self.usage_limited(json.dumps(envelope)):
            # Hit mid-run: the envelope still measured what it spent, so the
            # breaker is told a real figure rather than the reservation.
            raise UsageLimited(f"{self.name} reports a usage limit", usage)
        outcome = self._outcome(envelope)
        if outcome is not Outcome.COMPLETED:
            logger.warning(
                "%s run ended %s (subtype=%r)",
                self.name,
                outcome,
                envelope.get("subtype"),
            )
            return ReviewResult(findings=(), usage=usage, outcome=outcome)
        return ReviewResult(
            findings=self._findings(envelope["structured_output"]),
            usage=usage,
            outcome=outcome,
        )

    def _envelope(self, stdout: str) -> dict:
        """The single JSON object the CLI prints, or a loud failure."""
        try:
            envelope = json.loads(stdout)
        except ValueError as exc:
            raise EngineProtocolError(
                f"{self.name} printed something that is not JSON: {exc}"
            ) from exc
        if not isinstance(envelope, dict) or envelope.get("type") != "result":
            raise EngineProtocolError(
                f"{self.name} printed no result envelope: {stdout[:200]!r}"
            )
        return envelope

    @staticmethod
    def _outcome(envelope: dict) -> Outcome:
        """Which of the three ways this run ended."""
        subtype = envelope.get("subtype")
        if subtype in _TRUNCATING_SUBTYPES:
            return Outcome.TRUNCATED
        # A success without structured output is a failure too: the run
        # finished without producing the thing it was asked for.
        if subtype == "success" and isinstance(envelope.get("structured_output"), dict):
            return Outcome.COMPLETED
        return Outcome.FAILED

    def _usage(self, envelope: dict) -> Usage:
        """What the run cost, on every outcome including the failed ones.

        Cache reads are counted. They are cheaper than fresh input, not free,
        and the windows measure what a run consumed rather than what it was
        billed. ``total_cost_usd`` is on the envelope and deliberately not
        recorded: the windows are token-denominated.
        """
        reported = envelope.get("usage")
        reported = reported if isinstance(reported, dict) else {}
        tokens = sum(
            value
            for key, value in reported.items()
            if key.endswith("_tokens") and isinstance(value, int)
        )
        return Usage(
            tokens=tokens,
            confidence=UsageConfidence.EXACT,
            engine=self.name,
            model=self._model_used(envelope),
        )

    def _model_used(self, envelope: dict) -> str:
        """What the envelope says ran, falling back to what we asked for."""
        reported = envelope.get("modelUsage")
        if isinstance(reported, dict) and reported:
            return next(iter(reported))
        return self.model

    def _findings(self, structured: dict) -> tuple[Finding, ...]:
        """Turn validated output into findings, refusing anything malformed."""
        try:
            return tuple(
                Finding(
                    path=str(item["path"]),
                    line=int(item["line"]),
                    severity=Severity(item["severity"]),
                    body=str(item["body"]),
                )
                for item in structured["findings"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineProtocolError(
                f"{self.name} returned findings that do not fit the schema: {exc}"
            ) from exc
