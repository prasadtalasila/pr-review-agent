"""The one place this package shells out to ``git``.

Everything about a checkout is untrusted: the tree, its ``.gitattributes``,
its ``.gitmodules``. The controls live here rather than at each call site so
that they cannot be forgotten at one of them.

**The environment is the control, not the flags.** It is built from nothing
rather than inherited, which neutralises the host's own gitconfig in one
move -- and that file is where ``core.hooksPath``, ``core.fsmonitor``,
``diff.external``, credential helpers and smudge filters are all defined, so
one pair of variables covers mechanisms that would otherwise need a flag
each. ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_SYSTEM`` need git 2.32; below
that they are ignored *silently*, which is why the version is checked at
startup rather than assumed.

``GIT_ALLOW_PROTOCOL`` is a whitelist, and it overrides every ``protocol.*``
config key. It is deliberately a constant with no parameter, config key or
override: the ``-c protocol.allow=never`` alternative is defeated by any
specific ``protocol.ext.allow=always``, and an ``ext::`` submodule URL is
the shortest path from "checked out untrusted code" to "ran it".

The ``-c`` flags below are redundancy, with two exceptions that stop things
config-nulling does not: ``core.symlinks`` and ``transfer.fsckObjects``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from .._subprocess import communicate

logger = logging.getLogger(__name__)

#: The release that added ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM``.
MINIMUM_GIT_VERSION = (2, 32)

#: The only transport the agent will ever speak.
ALLOWED_PROTOCOL = "https"

#: The git every ``run_git`` runs. A plain name is resolved by the kernel
#: through the ``PATH`` this module deliberately passes through -- so a
#: writable directory early on the daemon user's ``PATH`` shadows it and
#: defeats ``HARDENING_FLAGS`` and ``git_environment`` together, without
#: touching either. ``workspace.git`` may name an absolute path instead;
#: ``use_git`` is how it gets here. Patched directly in tests.
#:
#: Module state rather than a parameter because ``run_git`` is a free
#: function called from a dozen places, and a binary threaded through all of
#: them would be the same value on every call. This is roadmap item A5.
GIT = "git"

GIT_TIMEOUT_SECONDS = 300.0

#: Passed through because production needs them. ``PATH`` is what finds
#: ``git-remote-https``; the proxy and CA variables are what a host behind a
#: TLS-inspecting firewall depends on, which is the deployment DESIGN.md
#: singles out. ``HOME`` is safe to pass because ``GIT_CONFIG_GLOBAL``
#: overrides ``$HOME/.gitconfig``.
_PASSTHROUGH = (
    "PATH",
    "HOME",
    "GIT_SSL_CAINFO",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)

HARDENING_FLAGS: tuple[str, ...] = (
    # A symlink in an untrusted tree that resolves outside it. The reviewer
    # reads the tree and can quote what it read into a public comment, so a
    # live symlink is an exfiltration path that needs no execution at all.
    # Nothing downstream can undo one, which makes it this layer's call.
    "-c",
    "core.symlinks=false",
    # Reject a malicious pack -- a tree containing `.GIT/`, a malformed
    # `.gitmodules` -- at index-pack time, at the boundary, rather than
    # discovering it during checkout.
    "-c",
    "transfer.fsckObjects=true",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "submodule.recurse=false",
    # Narrow by design: this neutralises a filter *named* `lfs` and nothing
    # else. Config nulling is what covers a filter named anything else.
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.required=false",
)


class WorkspaceError(RuntimeError):
    """Base for every failure this package raises."""


class GitCommandError(WorkspaceError):
    """A git invocation failed, timed out, or could not be started."""

    def __init__(self, argv: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)} exited {returncode}: {stderr}")


def git_environment() -> dict[str, str]:
    """The environment every git child gets, built rather than inherited."""
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": ALLOWED_PROTOCOL,
        # Parsed output, so the locale must not move under us.
        "LC_ALL": "C",
    }
    for name in _PASSTHROUGH:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def use_git(binary: str) -> None:
    """Point every later ``run_git`` at ``binary``.

    Called once at startup. A relative name still works -- it is the
    documented default, and requiring an absolute path would break every
    host where ``git`` is simply on the ``PATH`` -- but it says so, because
    a shadowed binary is invisible from the inside and the log line is the
    only place an operator meets the question.
    """
    global GIT  # pylint: disable=global-statement
    GIT = binary
    if not Path(binary).is_absolute():
        logger.warning(
            "workspace.git is %r, which resolves through PATH; "
            "an absolute path is what makes the checkout's hardening certain",
            binary,
        )


async def run_git(
    *args: str, cwd: Path | None = None, timeout: float = GIT_TIMEOUT_SECONDS
) -> str:
    """Run one git command under the hardened environment, returning stdout."""
    argv = (GIT, *HARDENING_FLAGS, *args)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=None if cwd is None else str(cwd),
            env=git_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise GitCommandError(argv, -1, str(exc)) from exc

    try:
        stdout, stderr = await communicate(process, timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise GitCommandError(argv, -1, f"timed out after {timeout}s") from exc

    if process.returncode:
        raise GitCommandError(
            argv, process.returncode, stderr.decode(errors="replace").strip()
        )
    return stdout.decode(errors="replace")


async def git_version() -> tuple[int, int]:
    """The installed git's ``(major, minor)``."""
    text = await run_git("--version")
    match = re.search(r"(\d+)\.(\d+)", text)
    if match is None:
        raise GitCommandError((GIT, "--version"), -1, f"unparsable version: {text!r}")
    return int(match.group(1)), int(match.group(2))
