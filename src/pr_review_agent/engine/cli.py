"""The subprocess boundary every CLI adapter shares.

A review runs over an attacker-controlled tree, so the containment is the
point of using a subprocess at all: its own working directory, an environment
built rather than inherited, and a wall clock it cannot outlive. Those three
are the same for ``claude``, ``codex`` and ``opencode``, so they live here;
everything downstream of "what did it print" is per-agent and belongs to a
subclass.

**The environment is an allowlist.** ``os.environ`` minus a denylist fails
open the moment a new variable appears, and the variable that must never
reach this process is the agent's GitHub credential -- a process reading an
attacker's tree and writing to a public comment is exactly the wrong place
for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from hashlib import sha256
from pathlib import Path

from ..budget import Usage
from .models import Capabilities, ReviewRequest, ReviewResult

logger = logging.getLogger(__name__)

#: How long a terminated agent gets to exit before the kill.
TERMINATE_GRACE_SECONDS = 5.0

#: Passed to every adapter's child. ``PATH`` is what finds the binary;
#: ``HOME`` is where a subscription credential lives. Nothing else is
#: inherited, and an adapter that needs more names it explicitly.
BASE_ENVIRONMENT = ("PATH", "HOME")


class EngineError(RuntimeError):
    """Base for every failure a CLI adapter raises."""


class EngineUnavailable(EngineError):
    """The binary is missing, or the subprocess could not be started.

    An adapter may raise this **only when nothing was executed**. The worker
    settles it at a provable zero rather than at the reserved ceiling, so an
    adapter that raised it after doing work would write a real spend into
    the ledger as nothing -- see ``docs/WORKER.md``.
    """


class EngineTimeout(EngineError):
    """The run outlived its wall clock and was killed.

    It carries no usage: the process was killed before it printed an
    envelope, so nothing measured what it spent. The reservation it leaves
    behind is the caller's to settle.
    """


class UsageLimited(EngineError):
    """The account's own usage limit was reached, not this run's ceiling.

    The one engine failure that must not be retried: the wall is the
    account's, so a second attempt reaches it again having spent to get
    there. It trips the circuit breaker instead -- see ``docs/BUDGET.md``.

    ``usage`` is what the run is known to have cost, and it is optional
    because a usage limit fails in two shapes. Refused up front, the CLI did
    no work and the spend is known to be *zero*, which the caller supplies.
    Hit mid-run, the CLI still printed an envelope and that envelope measured
    the spend, which travels here. Either way it is knowable, which is why
    this failure does not settle at the full reservation the way a run killed
    on its wall clock does.
    """

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class EngineProtocolError(EngineError):
    """The output could not be read.

    Raised rather than reported as an empty review. Parsing what a CLI
    prints is the cost of the subprocess boundary, and a format change that
    silently became "no findings" would look exactly like a clean review.
    """


def cli_environment(prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment a CLI child gets, built from nothing.

    ``prefixes`` admits a family of names an adapter needs -- an
    authentication prefix, typically -- without turning the allowlist back
    into a denylist.
    """
    env = {name: os.environ[name] for name in BASE_ENVIRONMENT if name in os.environ}
    for name, value in os.environ.items():
        if name.startswith(prefixes):
            env[name] = value
    return env


class CliEngine(ABC):
    """A review engine that is a command-line tool run as a subprocess."""

    #: Recorded on the ledger row, so a posted comment is traceable.
    name: str = "cli"

    #: Environment name prefixes this adapter needs beyond the base set.
    env_prefixes: tuple[str, ...] = ()

    def __init__(self, *, binary: str, timeout_seconds: float) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities:
        """What this engine can do."""

    @abstractmethod
    def argv(self, request: ReviewRequest) -> tuple[str, ...]:
        """The command line for one review, binary included."""

    @abstractmethod
    async def prompt(self, request: ReviewRequest) -> str:
        """The prompt for one review, delivered on stdin."""

    @abstractmethod
    def parse(self, stdout: str) -> ReviewResult:
        """Turn what the tool printed into a result."""

    async def preflight(self) -> None:  # noqa: B027 - opt-in, not obligatory
        """Whatever has to hold before the first review.

        Deliberately concrete and empty rather than abstract: an adapter with
        nothing to check should not be made to write an empty override, and a
        forgotten one would fail at import rather than where it matters.
        """

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Review the checkout, in a subprocess that cannot outlive its clock."""
        await self.preflight()
        argv = self.argv(request)
        prompt = await self.prompt(request)
        # The argv holds no secret -- the prompt is on stdin and any
        # credential is in the environment -- so it is logged whole. The
        # prompt is largely attacker-controlled and is logged as a digest:
        # a posted review stays traceable to the run that produced it
        # without copying the diff into the agent's own logs.
        logger.info(
            "running %s over %s: argv=%s prompt=sha256:%s",
            self.name,
            request.checkout.head_sha[:12],
            argv,
            sha256(prompt.encode()).hexdigest()[:16],
        )
        stdout = await self.run(argv, prompt, cwd=request.checkout.path)
        return self.parse(stdout)

    async def run(self, argv: tuple[str, ...], prompt: str, *, cwd: Path) -> str:
        """Run the tool over ``cwd``, feeding ``prompt`` on stdin.

        The prompt goes on stdin rather than in the argv because it carries
        the diff, and a diff-sized argv hits the platform limit on exactly
        the pull requests that most need reviewing.
        """
        process = await self._start(argv, cwd)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode()), self.timeout_seconds
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            await self._stop(process)
            raise EngineTimeout(
                f"{self.name} exceeded {self.timeout_seconds}s and was killed"
            ) from exc
        if process.returncode:
            complaint = stderr.decode(errors="replace").strip()
            if self.usage_limited(complaint):
                # Refused before doing any work, so the spend is known to be
                # nothing -- which is not the same as unknown, and the
                # difference is a reservation's worth of allowance.
                raise UsageLimited(f"{self.name} reports a usage limit: {complaint}")
            raise EngineProtocolError(
                f"{self.name} exited {process.returncode}: {complaint}"
            )
        return stdout.decode(errors="replace")

    def usage_limited(self, text: str) -> bool:
        """Whether ``text`` is this tool saying the account is out of quota.

        ``False`` here, because a tool that cannot say so has no such
        failure to recognise. An adapter that can overrides it -- and that
        override is the *only* thing standing between a real usage limit and
        an ordinary retry, so it is deliberately one small predicate rather
        than something spread across a parser.
        """
        del text
        return False

    async def _start(
        self, argv: tuple[str, ...], cwd: Path
    ) -> asyncio.subprocess.Process:
        """Launch the tool, or say plainly that it is not installed."""
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=cli_environment(self.env_prefixes),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # The cwd is named because it is the other thing that can be
            # missing here, and "cannot run 'claude'" sent the first reading
            # of exactly that failure to the wrong subsystem entirely.
            raise EngineUnavailable(f"cannot run {argv[0]!r} in {cwd}: {exc}") from exc

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        """Terminate, then kill, so the tool can close what it opened."""
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
        except (TimeoutError, asyncio.TimeoutError):
            process.kill()
            await process.wait()

    async def version(self) -> str:
        """Whatever ``<binary> --version`` prints, stripped."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "--version",
                env=cli_environment(self.env_prefixes),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise EngineUnavailable(f"cannot run {self.binary!r}: {exc}") from exc
        stdout, _ = await process.communicate()
        return stdout.decode(errors="replace").strip()
