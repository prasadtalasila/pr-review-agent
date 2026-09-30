"""The CLI engine suite's stand-in for a real `claude`.

`Recorder` answers the probe and the run with whatever a test hands it, and
records the argv, the environment and the stdin it was given -- which is
what lets the contract tests assert on the command line rather than on the
adapter's intentions.
"""

import asyncio
import json

import pytest

from pr_review_agent.budget import Mode
from pr_review_agent.engine import (
    ClaudeCliEngine,
    ReviewRequest,
)
from pr_review_agent.engine.claude import REQUIRED_FLAGS
from pr_review_agent.triggers.models import Trigger, TriggerKind
from pr_review_agent.workspace import Checkout, DiffSize, PullRequestFacts

FACTS = PullRequestFacts(
    number=7,
    head_sha="a" * 40,
    base_ref="main",
    additions=10,
    deletions=2,
    changed_files=1,
    state="open",
)

TRIGGER = Trigger(
    kind=TriggerKind.PR_OPENED,
    repo="o/r",
    pr_number=7,
    head_sha="a" * 40,
    actor_id=114395272,
    dedupe_key="o/r#7@" + "a" * 40,
)

#: Stands in for what `claude --help` prints. Only the flag names matter,
#: so it is the real shape without the prose: preflight reads it to prove
#: the containment flags it passes still exist.
HELP = "Usage: claude [options]\n" + "".join(
    f"  {flag} <value>\n" for flag in REQUIRED_FLAGS
)

USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 200,
    "cache_creation_input_tokens": 50,
    "cache_read_input_tokens": 10,
}


#: A valid assessment, which every completed run must carry (issue #126).
ASSESSMENT = {
    "effort": 2,
    "risk": "low",
    "recommendation": "safe_to_merge",
    "priority_files": ["src/x.py"],
}


def envelope(**overrides) -> str:
    """A result envelope shaped like the one the CLI prints."""
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "session_id": "0" * 32,
        "total_cost_usd": 0.12,
        "usage": USAGE,
        "modelUsage": {"claude-sonnet-5": {"inputTokens": 1_000}},
        "structured_output": {"assessment": ASSESSMENT, "findings": []},
    }
    data.update(overrides)
    return json.dumps(data)


def request(tmp_path, diff="--- a/x\n+++ b/x\n+pass\n") -> ReviewRequest:
    checkout = Checkout(
        path=tmp_path,
        head_sha="a" * 40,
        merge_base="b" * 40,
        diff=diff,
        reviewed=DiffSize(files=1, lines=1),
    )
    return ReviewRequest(
        checkout=checkout, facts=FACTS, trigger=TRIGGER, mode=Mode.FULL
    )


def engine(**kwargs) -> ClaudeCliEngine:
    kwargs.setdefault("model", "claude-sonnet-5")
    kwargs.setdefault("expected_version", "2.1.274")
    return ClaudeCliEngine(**kwargs)


class Recorder:
    """Stands in for the subprocess, recording what it was asked to run."""

    # It stands in for a whole process: what it was told, what it answers
    # with, and what was done to it. Splitting that to satisfy a count would
    # make the assertions harder to read, which is the opposite of the point.
    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        stdout: str = "",
        *,
        stderr: str = "",
        returncode: int | None = 0,
        hang: bool = False,
        version: str = "2.1.274 (Claude Code)",
        help_text: str = HELP,
        hang_on_probe: bool = False,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.hang = hang
        self.version = version
        self.help_text = help_text
        self.hang_on_probe = hang_on_probe
        self.probes = 0
        self.argv: tuple[str, ...] = ()
        self.env: dict[str, str] = {}
        self.cwd: str | None = None
        self.stdin = b""
        self.stopped = False

    async def __call__(self, *argv, cwd=None, env=None, **_kwargs):
        self.argv = argv
        self.cwd = cwd
        self.env = env or {}
        return self

    async def communicate(self, stdin=b""):
        # The two preflight probes run first, through the same patch point.
        if "--version" in self.argv or "--help" in self.argv:
            self.probes += 1
            if self.hang_on_probe:
                await asyncio.sleep(3600)
            if "--version" in self.argv:
                return self.version.encode(), b""
            return self.help_text.encode(), b""
        self.stdin = stdin
        if self.hang:
            await asyncio.sleep(3600)
        return self.stdout.encode(), self.stderr.encode()

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    async def wait(self):
        return self.returncode


@pytest.fixture
def run(monkeypatch):
    """Patch process creation, and hand the test back the recorder."""

    def install(recorder: Recorder) -> Recorder:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)
        return recorder

    return install
