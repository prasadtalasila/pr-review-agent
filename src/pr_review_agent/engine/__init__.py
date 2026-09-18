"""The swappable step: run a review over a checked-out pull request.

Everything else in the agent is agent-agnostic, so this is the only package
a second coding agent needs an implementation in. Adapters are command-line
tools -- ``claude``, ``codex``, ``opencode`` -- invoked as subprocesses; no
vendor SDK is linked. See ``docs/ENGINE.md``.
"""

from .claude import ClaudeCliEngine
from .cli import (
    CliEngine,
    EngineError,
    EngineProtocolError,
    EngineTimeout,
    EngineUnavailable,
)
from .fake import FULL, FakeEngine
from .models import (
    Capabilities,
    Finding,
    Outcome,
    ReviewEngine,
    ReviewRequest,
    ReviewResult,
    Severity,
)

__all__ = [
    "FULL",
    "Capabilities",
    "ClaudeCliEngine",
    "CliEngine",
    "EngineError",
    "EngineProtocolError",
    "EngineTimeout",
    "EngineUnavailable",
    "FakeEngine",
    "Finding",
    "Outcome",
    "ReviewEngine",
    "ReviewRequest",
    "ReviewResult",
    "Severity",
]
