"""Stopping a child that overran its clock: SIGTERM first, SIGKILL after."""

import asyncio
import sys

# Imported rather than spelled `asyncio.subprocess.Process`, which pylint
# resolves to the stdlib `subprocess` module and then reports as missing.
from asyncio.subprocess import Process

import pytest

from pr_review_agent._subprocess import communicate, stop

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the daemon is deployed on POSIX hosts; signals differ on Windows",
)

#: Ignores SIGTERM, so only the kill can end it.
_DEAF = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
)


async def _spawn(program: str) -> Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_communicate_returns_what_the_child_printed():
    process = await _spawn("import sys; sys.stdout.write(sys.stdin.read().upper())")
    stdout, _ = await communicate(process, b"ok", timeout=30.0)
    assert stdout == b"OK"


async def test_an_overrun_child_is_stopped_before_the_timeout_is_raised():
    process = await _spawn("import time; time.sleep(30)")
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await communicate(process, timeout=0.05)
    assert process.returncode is not None


async def test_a_child_that_ignores_sigterm_is_killed_once_the_grace_expires():
    process = await _spawn(_DEAF)
    await asyncio.sleep(0.2)  # let the handler be installed before the signal
    await stop(process, grace=0.1)
    assert process.returncode is not None


async def test_stopping_an_exited_child_is_a_no_op():
    process = await _spawn("pass")
    await process.wait()
    returncode = process.returncode
    await stop(process)
    assert process.returncode == returncode
