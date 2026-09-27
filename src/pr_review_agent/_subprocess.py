"""Running a child process under a wall clock it cannot outlive.

Two subsystems shell out -- the checkout to ``git``, a review to the agent
CLI -- and both need the same thing: talk to the child, and if it overruns,
stop it in a way that does not leave the filesystem wedged.
"""

from __future__ import annotations

import asyncio

__all__ = ["TERMINATE_GRACE_SECONDS", "communicate", "stop"]

#: How long a terminated child gets to exit before the kill.
TERMINATE_GRACE_SECONDS = 5.0


async def communicate(
    process: asyncio.subprocess.Process,
    stdin: bytes | None = None,
    *,
    timeout: float,
    grace: float = TERMINATE_GRACE_SECONDS,
) -> tuple[bytes, bytes]:
    """Feed ``stdin``, read the child out, and stop it if it overruns.

    Raises :exc:`asyncio.TimeoutError` once the child is gone, leaving the
    caller to name the failure in its own vocabulary: a timed-out ``git``
    and a timed-out review are different events to an operator.
    """
    try:
        return await asyncio.wait_for(process.communicate(stdin), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        await stop(process, grace=grace)
        raise


async def stop(
    process: asyncio.subprocess.Process,
    *,
    grace: float = TERMINATE_GRACE_SECONDS,
) -> None:
    """Terminate, then kill.

    ``kill()`` is SIGKILL, and a child killed under it never runs its
    cleanup -- git cannot remove its ``*.lock`` files, so a killed fetch can
    wedge the mirror until an operator deletes the lock by hand. SIGTERM
    first costs ``grace`` seconds at worst and avoids that.
    """
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), grace)
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
