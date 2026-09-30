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
from ..findings import Recommendation, Risk
from .cli import (
    CliEngine,
    EngineError,
    EngineProtocolError,
    EngineUnavailable,
    UsageLimited,
)
from .models import (
    Assessment,
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

#: Every long flag ``argv`` passes. Preflight refuses unless ``--help``
#: still lists all of them, because a flag the binary silently ignores is a
#: control that is not there. Spelled out rather than derived from a call to
#: ``argv``, which needs a request it has no use for; a test asserts the two
#: agree, so they cannot drift.
REQUIRED_FLAGS: tuple[str, ...] = (
    "--disable-slash-commands",
    "--json-schema",
    "--model",
    "--no-session-persistence",
    "--output-format",
    "--permission-mode",
    "--permission-prompts",
    "--restricted",
    "--setting-sources",
    "--strict-mcp-config",
    "--system-prompt",
    "--tools",
)

#: What the envelope's ``subtype`` means for publishability. Anything absent
#: from here is a failure: an unrecognised subtype is not a clean review.
_TRUNCATING_SUBTYPES = frozenset({"error_max_turns"})

#: How this adapter recognises the account being out of quota, as opposed to
#: this run being out of its own ceiling. Matched case-insensitively against
#: stderr, and against the *error-carrying fields only* of a result envelope
#: that says it errored -- never against `structured_output`, which is the
#: model's prose about a tree an attacker can write. A finding that quotes
#: `rate_limit_error` is an ordinary review, not a wall.
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


def _errored(envelope: dict) -> bool:
    """Whether the CLI itself says this run failed."""
    subtype = envelope.get("subtype")
    return envelope.get("is_error") is True or (
        isinstance(subtype, str) and subtype.startswith("error_")
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
        """Check the version, then *refuse* if the containment is gone.

        The two halves answer different questions and so end differently.

        An unexpected ``--version`` only warns: the parse is what protects
        against output changing shape and it already fails loudly, while
        refusing would take the reviewer offline on a routine upgrade that
        changed nothing we read.

        A missing containment flag refuses. ``--restricted``, ``--tools``
        and ``--permission-prompts`` are the whole sandbox -- if a future
        ``claude`` drops one, every argv control in this module is argv the
        binary ignores, and the review runs unconfined over an attacker's
        tree while the log says "output parsing may fail". Offline is the
        right failure for that. ``EngineUnavailable`` is the shape, because
        nothing was executed and the run settles at a provable zero.

        This is roadmap item A3.
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
        await self._verify_flags()

    async def _verify_flags(self) -> None:
        """Refuse unless every long flag this adapter passes is still real."""
        help_text = await self.help_text()
        missing = [flag for flag in REQUIRED_FLAGS if flag not in help_text]
        if missing:
            raise EngineUnavailable(
                f"{self.binary} --help does not list {', '.join(missing)}; "
                "refusing to review unconfined"
            )

    def usage_limited(self, text: str) -> bool:
        """Whether ``text`` is the CLI saying the *account* is out of quota."""
        lowered = text.lower()
        return any(marker in lowered for marker in _USAGE_LIMIT_MARKERS)

    def failure(self, returncode: int, stdout: str, stderr: str) -> EngineError:
        """Read the envelope a failed run prints on stdout, if it printed one.

        ``--output-format json`` reports a failed run as an ``is_error``
        envelope on stdout and an exit of 1, with nothing on stderr. Reading
        only stderr logged ``claude exited 1:`` and nothing else, and --
        worse -- hid a usage limit hit mid-run from the detector, so it was
        retried into the same wall instead of tripping the breaker.
        """
        try:
            envelope = self._envelope(stdout)
        except EngineProtocolError:
            return super().failure(returncode, stdout, stderr)
        if self._envelope_usage_limited(envelope):
            return UsageLimited(
                f"{self.name} reports a usage limit", self._usage(envelope)
            )
        complaint = " ".join(
            part for part in (stderr, self._envelope_complaint(envelope)) if part
        )
        failure = super().failure(returncode, stdout, complaint)
        if isinstance(failure, EngineProtocolError):
            # The envelope measured the run even though the run failed, so
            # the spend is known and the reservation would overstate it.
            failure.usage = self._usage(envelope)
            status = envelope.get("api_error_status")
            failure.status = status if isinstance(status, int) else None
        return failure

    @staticmethod
    def _envelope_complaint(envelope: dict) -> str:
        """The CLI's own account of an errored envelope, or nothing.

        Asked of the same fields, under the same rule, as the usage-limit
        detector: an envelope that does not say it errored has no error to
        quote, and its ``result`` may be model prose.
        """
        if not _errored(envelope):
            return ""
        fields = (
            envelope.get("api_error_status"),
            envelope.get("error"),
            envelope.get("result"),
        )
        return (
            f"{envelope.get('subtype')}: "
            + " ".join(str(f) for f in fields if f is not None)[:200]
        )

    def _envelope_usage_limited(self, envelope: dict) -> bool:
        """Whether *the CLI itself* said the account is out of quota.

        Only an envelope that reports an error is asked, and only its own
        error-carrying fields are read. The review's output is not one of
        them: it is model prose about an attacker-influenced tree, and
        matching a marker there lets one line in a diff trip the breaker for
        the whole fleet.
        """
        if not _errored(envelope):
            return False
        return any(
            self.usage_limited(field if isinstance(field, str) else json.dumps(field))
            for field in (
                envelope.get("error"),
                envelope.get("result"),
                envelope.get("subtype"),
            )
            if field is not None
        )

    def parse(self, stdout: str) -> ReviewResult:
        """Read the result envelope, strictly."""
        envelope = self._envelope(stdout)
        usage = self._usage(envelope)
        if self._envelope_usage_limited(envelope):
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
        structured = envelope["structured_output"]
        return ReviewResult(
            findings=self._findings(structured),
            usage=usage,
            outcome=outcome,
            assessment=self._assessment(structured),
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
        """Turn validated output into findings, refusing anything malformed.

        ``number`` is optional and absent means "new this round". A
        non-integer is a protocol error like any other; a *wrong* integer is
        not this layer's problem, because ``numbering.assign`` refuses a
        number that was never issued on this pull request.
        """
        try:
            return tuple(
                Finding(
                    path=str(item["path"]),
                    line=int(item["line"]),
                    severity=Severity(item["severity"]),
                    title=str(item["title"]),
                    body=str(item["body"]),
                    number=None if item.get("number") is None else int(item["number"]),
                )
                for item in structured["findings"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineProtocolError(
                f"{self.name} returned findings that do not fit the schema: {exc}"
            ) from exc

    def _assessment(self, structured: dict) -> Assessment:
        """The required assessment, or a protocol error like a bad finding.

        Missing is refused rather than tolerated: the schema requires it, so
        output without it is output that did not follow the schema.
        """
        try:
            item = structured["assessment"]
            return Assessment(
                effort=int(item["effort"]),
                risk=Risk(item["risk"]),
                recommendation=Recommendation(item["recommendation"]),
                priority_files=tuple(str(path) for path in item["priority_files"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineProtocolError(
                f"{self.name} returned an assessment that does not fit the schema: "
                f"{exc}"
            ) from exc
