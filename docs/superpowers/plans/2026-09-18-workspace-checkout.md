# Pull Request Checkout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a pull request's code on disk at an exact commit, with a merge-base diff, without executing any of it and without letting it read outside itself — then take it away again.

**Architecture:** A `workspace` package over one bare mirror per repository. Every `git` invocation goes through a single hardened runner in `gitcmd.py` that builds the child environment explicitly; `repo.py` owns the mirror, the per-run detached worktree and the teardown. The subsystem lands with **no caller**: the worker that will use it does not exist yet.

**Tech Stack:** Python 3.10–3.14, asyncio (`create_subprocess_exec`), the `git` CLI ≥ 2.32, pytest with `asyncio_mode = "auto"`, `cryptography` (dev-only) for the test fixture's certificate.

**Spec:** `docs/superpowers/specs/2026-09-18-git-checkout-design.md`

## Global Constraints

- **Nothing may call a review engine outside the budget governor.** This branch calls no engine and does not call `ReviewQueue.claim()`. (`CLAUDE.md` §5)
- **The two size caps are spending bounds.** They live in `budget`, their defaults are pinned by a test, and widening one must be a visible diff.
- **Allowlisting is on the numeric GitHub user id, never the login.** No new trust check is introduced here; do not add one.
- **PR bodies, comment bodies, diffs and now the checked-out tree are untrusted input.** Nothing here may widen what the agent is allowed to do.
- `GIT_ALLOW_PROTOCOL` is the constant `"https"`. There is **no** parameter, config key or environment override that widens it. A test pins that `file://` and `http://` are refused.
- Supported Python range is `>=3.10,<3.15`; `target-version = "py310"`. No 3.11+ syntax. `enum.StrEnum` comes from `._compat`, never from `enum`.
- Line length 88 (ruff). Ruff lint rules: `E`, `F`, `I`, `UP`, `B`, `SIM`.
- Pyright runs over **`src` and `tests`** in `basic` mode — test helpers must type-check too.
- Pylint must score ≥ 9.0 on `src` and on `tests`.
- The full local gate before claiming done: `poetry run pytest`, `poetry run ruff check .`, `poetry run ruff format --check .`, `poetry run pylint src --rcfile=.pylintrc --fail-under=9.0`, `poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 --disable=missing-function-docstring,missing-module-docstring`, `poetry run pyright src tests`. Quote the result; never predict it.
- The suite needs no network egress and spends no tokens. The git tests talk to a loopback TLS socket; nothing leaves the host.
- Git-backed tests are POSIX-only (`pytest.mark.skipif(sys.platform == "win32")`). The daemon is deployed on Linux; Git for Windows differs in exec-path layout and `/dev/null` handling, and the Windows CI job spot-checks the pure-Python parts. Everywhere else they **fail** rather than skip when git is missing or below 2.32.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
| :-- | :-- |
| `src/pr_review_agent/workspace/__init__.py` | Public surface: `Workspace`, `Checkout`, `PullRequestFacts`, the error types |
| `src/pr_review_agent/workspace/gitcmd.py` | The hardened runner: explicit environment, `-c` flags, timeout, terminate-then-kill |
| `src/pr_review_agent/workspace/repo.py` | `PullRequestFacts`, `Checkout`, `Workspace`: mirror, sweep, size gate, fetch, worktree, diff, teardown |
| `src/pr_review_agent/poller/pulls.py` | One `GET /repos/{o}/{n}/pulls/{n}` and its mapping to `PullRequestFacts` |
| `tests/conftest.py` | The HTTPS double: certificate, `git http-backend` over a loopback TLS socket |
| `tests/test_gitcmd.py` | The runner in isolation |
| `tests/test_workspace.py` | Checkout behaviour, bounds, teardown, concurrency |
| `tests/test_workspace_safety.py` | The four escapes that must not fire |

`PullRequestFacts` is defined in `workspace/repo.py` and imported by `poller/pulls.py`, in the same direction as `poller/payloads.py` importing `triggers/models.py`. Nothing in `workspace/` imports `poller/`.

---

### Task 1: The two caps and the `workspace` section

**Files:**
- Modify: `src/pr_review_agent/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `BudgetConfig.max_changed_files: int` and `BudgetConfig.max_changed_lines: int` (defaults `100` / `5000`), module constants `DEFAULT_MAX_CHANGED_FILES`, `DEFAULT_MAX_CHANGED_LINES`, `DEFAULT_CACHE_DIR = ".cache/repos"`; `WorkspaceConfig(cache_dir: str)` with `Config.workspace`. Task 5 reads the caps; Task 8 reads `cache_dir`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`:

```python
def test_size_caps_default_to_the_pinned_values(tmp_path):
    config = Config.from_mapping(_minimal())
    assert config.budget.max_changed_files == 100
    assert config.budget.max_changed_lines == 5000


def test_size_caps_can_be_tightened(tmp_path):
    data = _minimal()
    data["budget"]["max_changed_files"] = 10
    data["budget"]["max_changed_lines"] = 200
    config = Config.from_mapping(data)
    assert config.budget.max_changed_files == 10
    assert config.budget.max_changed_lines == 200


def test_a_non_positive_cap_is_rejected():
    data = _minimal()
    data["budget"]["max_changed_lines"] = 0
    with pytest.raises(ConfigError, match="max_changed_lines"):
        Config.from_mapping(data)


def test_workspace_section_is_optional():
    assert Config.from_mapping(_minimal()).workspace.cache_dir == ".cache/repos"


def test_workspace_cache_dir_is_read():
    data = _minimal()
    data["workspace"] = {"cache_dir": "/srv/agent/repos"}
    assert Config.from_mapping(data).workspace.cache_dir == "/srv/agent/repos"


def test_unknown_workspace_key_is_rejected():
    data = _minimal()
    data["workspace"] = {"cachedir": "/srv"}
    with pytest.raises(ConfigError, match="workspace"):
        Config.from_mapping(data)
```

Use whatever minimal-mapping helper `tests/test_config.py` already has; if it builds the dict inline, add `_minimal()` returning a fresh copy each call.

- [ ] **Step 2: Run to verify they fail**

Run: `poetry run pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: 'BudgetConfig' object has no attribute 'max_changed_files'`.

- [ ] **Step 3: Implement**

In `config.py`, beside the existing budget constants:

```python
#: Layer 2's diff-size caps. Unlike the plan token counts, a diff-size cap
#: does not depend on an unpublished quota, so a default is an engineering
#: choice rather than a fabricated ceiling.
DEFAULT_MAX_CHANGED_FILES = 100
DEFAULT_MAX_CHANGED_LINES = 5000

#: Where the bare mirrors and per-run worktrees live.
DEFAULT_CACHE_DIR = ".cache/repos"


def _positive(data: dict, key: str, default: int) -> int:
    """Read an optional positive integer from the ``budget`` section."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"budget.{key} must be a positive integer")
    return value
```

Add the two fields to `BudgetConfig` after `reviewer_share_pct`:

```python
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES
```

and in `BudgetConfig.parse`, pass them into the `cls(...)` call:

```python
            max_changed_files=_positive(
                data, "max_changed_files", DEFAULT_MAX_CHANGED_FILES
            ),
            max_changed_lines=_positive(
                data, "max_changed_lines", DEFAULT_MAX_CHANGED_LINES
            ),
```

Add the section class:

```python
@dataclass(frozen=True)
class WorkspaceConfig:
    """Where checkouts live on disk."""

    cache_dir: str = DEFAULT_CACHE_DIR

    @classmethod
    def parse(cls, data: dict) -> WorkspaceConfig:
        """Validate the ``workspace`` section."""
        cache_dir = data.get("cache_dir", DEFAULT_CACHE_DIR)
        if not isinstance(cache_dir, str) or not cache_dir.strip():
            raise ConfigError(
                f"workspace.cache_dir must be a non-empty path, got {cache_dir!r}"
            )
        return cls(cache_dir=cache_dir)
```

In `Config`: add the field `workspace: WorkspaceConfig`, add `"workspace"` to the allowed top-level set, extend the `budget` allowed-key set with `"max_changed_files"` and `"max_changed_lines"`, and build it like `store`:

```python
            # Optional for the same reason as `store`: it holds a path and
            # nothing else. The cap that could spend lives in `budget`.
            workspace=WorkspaceConfig.parse(
                _section(data, "workspace", {"cache_dir"})
                if "workspace" in data
                else {}
            ),
```

- [ ] **Step 4: Run to verify they pass**

Run: `poetry run pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Update `config.example.yaml`**

Under `budget:`, after `reviewer_share_pct`:

```yaml
  # Layer 2's diff-size caps: a pull request bigger than either is refused
  # before anything is fetched. Both exist because neither bounds the other
  # -- 2000 one-line files pass a line cap and still bury the engine.
  # Optional; these are the defaults. Reloaded on SIGHUP with the rest of
  # this section.
  max_changed_files: 100
  max_changed_lines: 5000
```

And a new section at the end:

```yaml
workspace:
  # Where the bare mirror and the per-run checkouts live. Optional; this is
  # the default. Created 0700. A relative path is resolved against the
  # working directory the daemon starts in, so prefer an absolute path under
  # a service account's data directory in production.
  cache_dir: .cache/repos
```

- [ ] **Step 6: Commit**

```bash
git add src/pr_review_agent/config.py tests/test_config.py config.example.yaml
git commit -m "Add the diff-size caps and the workspace section"
```

---

### Task 2: Resolving a pull request's facts

**Files:**
- Modify: `src/pr_review_agent/poller/endpoints.py`
- Create: `src/pr_review_agent/poller/pulls.py`
- Modify: `src/pr_review_agent/poller/payloads.py` (docstring only)
- Test: `tests/test_endpoints.py`, `tests/test_pulls.py`

**Interfaces:**
- Consumes: `PullRequestFacts` from Task 3's `workspace/repo.py`. **Implement Task 3 first if working strictly in order**; if not, define `PullRequestFacts` exactly as given below.
- Produces: `RepoEndpoints.pull(number: int) -> str`; `pull_request_facts(payload: dict) -> PullRequestFacts`; `async fetch_pull_request_facts(client: GitHubClient, endpoints: RepoEndpoints, number: int) -> PullRequestFacts`.

- [ ] **Step 1: Write the failing tests**

`tests/test_endpoints.py`:

```python
def test_single_pull_request_path():
    endpoints = RepoEndpoints(owner="prasadtalasila", name="pr-review-agent")
    assert endpoints.pull(7) == "/repos/prasadtalasila/pr-review-agent/pulls/7"
```

New `tests/test_pulls.py`:

```python
"""The single-pull-request read that resolves head_sha and the size numbers."""

import httpx
import pytest

from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.poller.pulls import fetch_pull_request_facts, pull_request_facts
from pr_review_agent.triggers.models import PayloadError

PAYLOAD = {
    "number": 7,
    "head": {"sha": "a" * 40},
    "base": {"ref": "main"},
    "additions": 12,
    "deletions": 3,
    "changed_files": 2,
}


def test_facts_are_mapped_from_the_payload():
    facts = pull_request_facts(PAYLOAD)
    assert facts.number == 7
    assert facts.head_sha == "a" * 40
    assert facts.base_ref == "main"
    assert facts.changed_lines == 15


def test_a_missing_head_sha_is_a_payload_error():
    payload = {**PAYLOAD, "head": {}}
    with pytest.raises(PayloadError):
        pull_request_facts(payload)


async def test_fetch_resolves_head_sha_for_a_mention():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=PAYLOAD, headers={"etag": '"x"'})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    endpoints = RepoEndpoints(owner="o", name="n")
    facts = await fetch_pull_request_facts(client, endpoints, 7)
    await client.aclose()

    assert seen == ["/repos/o/n/pulls/7"]
    assert facts.head_sha == "a" * 40
```

Match `GitHubClient`'s real constructor and close method — read `tests/test_client.py` and copy how it builds a client over a `MockTransport`, rather than assuming the signature above.

- [ ] **Step 2: Run to verify they fail**

Run: `poetry run pytest tests/test_endpoints.py tests/test_pulls.py -q`
Expected: FAIL — `ModuleNotFoundError: pr_review_agent.poller.pulls`.

- [ ] **Step 3: Implement**

`endpoints.py`, a method on `RepoEndpoints`:

```python
    def pull(self, number: int) -> str:
        """The path for one pull request.

        Not one of the watched endpoints: this is read once per claimed
        trigger, to resolve ``head_sha`` and the size numbers the checkout
        gates on, never on the polling cycle.
        """
        return f"/repos/{self.owner}/{self.name}/pulls/{number}"
```

New `src/pr_review_agent/poller/pulls.py`:

```python
"""Read one pull request, for the two things the checkout needs.

``head_sha`` is ``None`` for a mention trigger -- the comment payload does
not carry one, as ``payloads.py`` explains -- and the size gate needs the
`additions` / `deletions` / `changed_files` counts that only the
single-pull-request endpoint reports. Both come from the same read, which
is why it is one request rather than two.
"""

from __future__ import annotations

from ..triggers.models import PayloadError
from ..workspace import PullRequestFacts
from .client import GitHubClient
from .endpoints import RepoEndpoints


def pull_request_facts(payload: dict) -> PullRequestFacts:
    """Map a single-pull-request payload onto the facts a checkout needs."""
    try:
        return PullRequestFacts(
            number=int(payload["number"]),
            head_sha=str(payload["head"]["sha"]),
            base_ref=str(payload["base"]["ref"]),
            additions=int(payload["additions"]),
            deletions=int(payload["deletions"]),
            changed_files=int(payload["changed_files"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PayloadError(f"unusable pull request payload: {exc}") from exc


async def fetch_pull_request_facts(
    client: GitHubClient, endpoints: RepoEndpoints, number: int
) -> PullRequestFacts:
    """Read one pull request and map it."""
    result = await client.get(endpoints.pull(number))
    if result.payload is None:
        raise PayloadError(f"no payload for pull request {number}")
    return pull_request_facts(result.payload)
```

Adjust the `client.get(...)` call and the result attribute to the real `PollResult` shape.

- [ ] **Step 4: Point the `payloads.py` docstring at this module**

Replace "``head_sha`` is therefore left unresolved and read when the trigger is claimed." with "``head_sha`` is therefore left unresolved here and read when the trigger is claimed, by ``poller/pulls.py``, which needs the same request for the checkout's size gate."

- [ ] **Step 5: Run to verify they pass**

Run: `poetry run pytest tests/test_endpoints.py tests/test_pulls.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pr_review_agent/poller tests/test_endpoints.py tests/test_pulls.py
git commit -m "Resolve a pull request's head sha and size numbers"
```

---

### Task 3: The hardened git runner

**Files:**
- Create: `src/pr_review_agent/workspace/__init__.py`, `src/pr_review_agent/workspace/gitcmd.py`
- Test: `tests/test_gitcmd.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WorkspaceError`, `GitCommandError(WorkspaceError)` with `.argv`, `.returncode`, `.stderr`; `async run_git(*args: str, cwd: Path | None = None, timeout: float = GIT_TIMEOUT_SECONDS) -> str` returning stdout; constants `MINIMUM_GIT_VERSION = (2, 32)`, `HARDENING_FLAGS: tuple[str, ...]`, `ALLOWED_PROTOCOL = "https"`; `async git_version() -> tuple[int, int]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_gitcmd.py`:

```python
"""The one place the agent shells out, and the environment it builds."""

import sys

import pytest

from pr_review_agent.workspace.gitcmd import (
    ALLOWED_PROTOCOL,
    GitCommandError,
    git_environment,
    git_version,
    run_git,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the daemon is deployed on POSIX hosts"
)


def test_the_environment_is_built_not_inherited(monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "touch /tmp/pwned")
    monkeypatch.setenv("SOMETHING_ELSE", "1")
    env = git_environment()
    assert "GIT_SSH_COMMAND" not in env
    assert "SOMETHING_ELSE" not in env


def test_the_protocol_whitelist_is_https_only():
    assert git_environment()["GIT_ALLOW_PROTOCOL"] == ALLOWED_PROTOCOL == "https"


def test_config_is_neutralised_and_home_still_passed(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    env = git_environment()
    assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"]
    assert env["HOME"] == "/home/someone"


async def test_run_git_returns_stdout():
    assert "git version" in await run_git("--version")


async def test_a_failing_command_raises_with_its_stderr(tmp_path):
    with pytest.raises(GitCommandError) as excinfo:
        await run_git("rev-parse", "HEAD", cwd=tmp_path)
    assert excinfo.value.returncode != 0
    assert "rev-parse" in str(excinfo.value)


async def test_a_timeout_raises_rather_than_hanging(tmp_path):
    with pytest.raises(GitCommandError, match="timed out"):
        # `git help --all` is not slow; a zero timeout is what is being
        # pinned -- that the deadline is enforced at all.
        await run_git("help", "--all", cwd=tmp_path, timeout=0.0)


async def test_the_git_version_is_readable():
    major, minor = await git_version()
    assert (major, minor) >= (2, 0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `poetry run pytest tests/test_gitcmd.py -q`
Expected: FAIL — `ModuleNotFoundError: pr_review_agent.workspace`.

- [ ] **Step 3: Implement `gitcmd.py`**

```python
"""The one place this package shells out to ``git``.

Everything about the checkout is untrusted: the tree, its ``.gitattributes``,
its ``.gitmodules``. The controls live here rather than at each call site so
they cannot be forgotten at one of them.

**The environment is the control.** It is built from nothing rather than
inherited, so the host's own gitconfig -- where ``core.hooksPath``,
``core.fsmonitor``, ``diff.external``, credential helpers and smudge filters
are all defined -- is neutralised in one move by ``GIT_CONFIG_GLOBAL`` and
``GIT_CONFIG_SYSTEM``. That pair needs git >= 2.32; below it they are ignored
silently, which is why the version is checked at startup.

``GIT_ALLOW_PROTOCOL`` is a whitelist that overrides every ``protocol.*``
config key. It is a constant, deliberately: the ``-c protocol.allow=never``
alternative is overridden by any specific ``protocol.ext.allow=always``, and
an ``ext::`` submodule URL is the shortest path from "checked out untrusted
code" to "ran it".

The ``-c`` flags below are redundancy, except ``core.symlinks`` and
``transfer.fsckObjects``, which stop things config-nulling does not.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_SYSTEM`` arrived in this release.
MINIMUM_GIT_VERSION = (2, 32)

#: The only transport the agent will ever speak.
ALLOWED_PROTOCOL = "https"

GIT_TIMEOUT_SECONDS = 300.0
#: How long a terminated git gets to remove its own lock files before the
#: kill. SIGKILL leaves ``*.lock`` behind; SIGTERM does not.
TERMINATE_GRACE_SECONDS = 5.0

#: Passed through because production needs them: ``PATH`` finds
#: ``git-remote-https``, and the proxy and CA variables are what a host
#: behind a TLS-inspecting firewall depends on. ``HOME`` is safe because
#: ``GIT_CONFIG_GLOBAL`` overrides ``$HOME/.gitconfig``.
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
    # A symlink in an untrusted tree resolving outside it: the reviewer
    # would read the target and could quote it into a public comment.
    # Nothing downstream can undo a symlink, so it is refused here.
    "-c", "core.symlinks=false",
    # Reject a malicious pack -- a tree containing `.GIT/`, a malformed
    # `.gitmodules` -- at index-pack, at the boundary.
    "-c", "transfer.fsckObjects=true",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "submodule.recurse=false",
    # Narrow by design: this neutralises a filter *named* `lfs`. Config
    # nulling is what covers a filter named anything else.
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
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
        "LC_ALL": "C",
    }
    for name in _PASSTHROUGH:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


async def run_git(
    *args: str, cwd: Path | None = None, timeout: float = GIT_TIMEOUT_SECONDS
) -> str:
    """Run one git command under the hardened environment, returning stdout."""
    argv = ("git", *HARDENING_FLAGS, *args)
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
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        await _stop(process)
        raise GitCommandError(argv, -1, f"timed out after {timeout}s") from exc

    if process.returncode:
        raise GitCommandError(
            argv, process.returncode or -1, stderr.decode(errors="replace").strip()
        )
    return stdout.decode(errors="replace")


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Terminate, then kill.

    ``kill()`` is SIGKILL, and git removes its ``*.lock`` files on SIGTERM
    but cannot on SIGKILL -- a killed fetch can wedge the mirror until an
    operator removes the lock by hand.
    """
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
    except (asyncio.TimeoutError, TimeoutError):
        process.kill()
        await process.wait()


async def git_version() -> tuple[int, int]:
    """The installed git's ``(major, minor)``."""
    text = await run_git("--version")
    match = re.search(r"(\d+)\.(\d+)", text)
    if match is None:
        raise GitCommandError(("git", "--version"), -1, f"unparsable: {text!r}")
    return int(match.group(1)), int(match.group(2))
```

`workspace/__init__.py`:

```python
"""Put a pull request's code on disk at an exact commit, then take it away."""

from .gitcmd import GitCommandError, WorkspaceError
from .repo import Checkout, PullRequestFacts, PullRequestTooLarge, Workspace

__all__ = [
    "Checkout",
    "GitCommandError",
    "PullRequestFacts",
    "PullRequestTooLarge",
    "Workspace",
    "WorkspaceError",
]
```

Write `__init__.py` last, after Task 5 creates `repo.py`, or the import fails.

- [ ] **Step 4: Run to verify they pass**

Run: `poetry run pytest tests/test_gitcmd.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pr_review_agent/workspace tests/test_gitcmd.py
git commit -m "Add the hardened git runner"
```

---

### Task 4: The HTTPS test double

**Files:**
- Create: `tests/conftest.py` (or extend the existing one)
- Modify: `pyproject.toml` (dev dependency), `poetry.lock`
- Test: `tests/test_double.py`

**Interfaces:**
- Consumes: nothing.
- Produces: pytest fixtures `git_remote` yielding a `GitRemote` with `.url: str`, `.ca: Path`, `.requests: list[dict[str, str]]`, `.head_sha: str`, `.base_ref: str`; and `origin_repo` building the fixture repository. Tasks 5–7 consume `git_remote`.

The double exists because `GIT_ALLOW_PROTOCOL=https` refuses `file://` — so tests must speak the production transport, which also makes "no credential reaches the wire" assertable.

- [ ] **Step 1: Add the dev dependency**

```bash
poetry add --group dev "cryptography>=42"
```

Then check it: `poetry check --lock`.

- [ ] **Step 2: Write the fixture**

In `tests/conftest.py`:

```python
"""A local HTTPS remote, so the git tests speak the production transport.

`GIT_ALLOW_PROTOCOL=https` refuses `file://`, which is the point: the
whitelist is a constant with no test-only widening. Serving `git
http-backend` behind a loopback TLS socket keeps the tests offline while
still exercising `git-remote-https` -- and it makes the wire observable,
so "no credential is sent" is an assertion rather than a claim.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import os
import pathlib
import ssl
import subprocess
import threading
from dataclasses import dataclass, field

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

PR_NUMBER = 7


def _write_cert(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    key_path = directory / "key.pem"
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, key_path


def git(*args: str, cwd: pathlib.Path | None = None) -> str:
    """Plain git, for building fixtures. Not the hardened runner."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "T",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "T",
           "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _backend() -> str:
    """`git http-backend`, found through git's own exec path."""
    exec_path = pathlib.Path(git("--exec-path"))
    for candidate in ("git-http-backend", "git-http-backend.exe"):
        if (exec_path / candidate).exists():
            return str(exec_path / candidate)
    raise RuntimeError(f"git-http-backend not found under {exec_path}")


@dataclass
class GitRemote:
    """A loopback HTTPS remote serving one bare repository."""

    url: str
    ca: pathlib.Path
    serve_root: pathlib.Path
    head_sha: str
    base_ref: str = "main"
    requests: list[dict[str, str]] = field(default_factory=list)
```

Then the handler and the fixture:

```python
def _make_handler(root: pathlib.Path, seen: list[dict[str, str]]):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: A002 - silence the test server
            pass

        def _cgi(self, body: bytes = b"") -> None:
            seen.append({k: v for k, v in self.headers.items()})
            path, _, query = self.path.partition("?")
            env = {
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "PATH": os.environ["PATH"],
            }
            protocol = self.headers.get("Git-Protocol")
            if protocol:
                env["HTTP_GIT_PROTOCOL"] = protocol
            out = subprocess.run(
                [_backend()], input=body, env=env, capture_output=True
            ).stdout
            head, _, payload = out.partition(b"\r\n\r\n")
            self.send_response(200)
            for line in head.split(b"\r\n"):
                if b":" in line:
                    key, value = line.split(b":", 1)
                    self.send_header(key.decode(), value.decode().strip())
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
            self._cgi()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
            self._cgi(self.rfile.read(int(self.headers["Content-Length"])))

    return Handler


@pytest.fixture
def git_remote(tmp_path_factory) -> GitRemote:
    """A fork-shaped pull request, served over https from loopback."""
    root = tmp_path_factory.mktemp("remote")
    build = root / "build"
    serve = root / "serve" / "owner" / "name.git"
    serve.parent.mkdir(parents=True)

    build.mkdir()
    git("init", "-q", "-b", "main", str(build))
    (build / "base.txt").write_text("base\n")
    git("add", "-A", cwd=build)
    git("commit", "-qm", "base", cwd=build)
    # A commit on no branch: exactly the shape of a fork's pull request head.
    git("checkout", "-q", "-b", "pr", cwd=build)
    (build / "feature.py").write_text("def added():\n    return 1\n")
    git("add", "-A", cwd=build)
    git("commit", "-qm", "pr head", cwd=build)
    head_sha = git("rev-parse", "HEAD", cwd=build)
    git("checkout", "-q", "main", cwd=build)
    git("branch", "-q", "-D", "pr", cwd=build)

    git("clone", "-q", "--bare", str(build), str(serve))
    git("update-ref", f"refs/pull/{PR_NUMBER}/head", head_sha, cwd=serve)

    ca, key = _write_cert(root)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, key)
    seen: list[dict[str, str]] = []
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_handler(root / "serve", seen)
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    remote = GitRemote(
        url=f"https://127.0.0.1:{server.server_address[1]}/owner/name.git",
        ca=ca,
        serve_root=serve,
        head_sha=head_sha,
        requests=seen,
    )
    try:
        yield remote
    finally:
        server.shutdown()
        server.server_close()
```

Tests using it must set `GIT_SSL_CAINFO` via `monkeypatch.setenv`, because the runner passes it through.

- [ ] **Step 3: Write a test of the double itself**

`tests/test_double.py`:

```python
"""The fixture is load-bearing, so it gets its own test."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the daemon is deployed on POSIX hosts"
)


def test_the_double_serves_the_fork_shaped_ref(git_remote, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    out = subprocess.run(
        ["git", "ls-remote", git_remote.url, "refs/pull/7/head"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert git_remote.head_sha in out
    assert git_remote.requests, "the double saw no requests"
```

- [ ] **Step 4: Run**

Run: `poetry run pytest tests/test_double.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_double.py pyproject.toml poetry.lock
git commit -m "Serve the git fixtures over https from loopback"
```

---

### Task 5: The workspace — mirror, gate, fetch, worktree, diff, teardown

**Files:**
- Create: `src/pr_review_agent/workspace/repo.py`
- Modify: `src/pr_review_agent/workspace/__init__.py`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: `run_git`, `GitCommandError`, `WorkspaceError` (Task 3); `git_remote` (Task 4).
- Produces: `PullRequestFacts`, `Checkout(path, head_sha, merge_base, diff)`, `PullRequestTooLarge`, `Workspace(repo, cache_dir, base_url=GITHUB_BASE)` with `async sweep()` and the `checkout(facts, *, max_changed_files, max_changed_lines)` async context manager.

- [ ] **Step 1: Write the failing tests**

`tests/test_workspace.py`:

```python
"""One bare mirror, a detached worktree per run, and nothing left behind."""

import asyncio
import sys

import pytest

from pr_review_agent.workspace import (
    PullRequestFacts,
    PullRequestTooLarge,
    Workspace,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the daemon is deployed on POSIX hosts"
)

CAPS = {"max_changed_files": 100, "max_changed_lines": 5000}


def facts(remote, **overrides) -> PullRequestFacts:
    values = {
        "number": 7,
        "head_sha": remote.head_sha,
        "base_ref": remote.base_ref,
        "additions": 2,
        "deletions": 0,
        "changed_files": 1,
    }
    values.update(overrides)
    return PullRequestFacts(**values)


@pytest.fixture
def workspace(git_remote, tmp_path, monkeypatch) -> Workspace:
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    return Workspace(
        repo="owner/name", cache_dir=tmp_path / "cache", base_url=git_remote.url
    )


async def test_a_fork_shaped_head_is_checked_out_at_its_exact_sha(
    workspace, git_remote
):
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert checkout.head_sha == git_remote.head_sha
        assert (checkout.path / "feature.py").read_text().startswith("def added")


async def test_the_diff_is_against_the_merge_base(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert "def added():" in checkout.diff
        assert checkout.merge_base != checkout.head_sha


async def test_head_sha_is_resolved_when_the_trigger_carried_none(
    workspace, git_remote
):
    # A mention trigger's queue row has head_sha NULL; the facts read is what
    # fills it, and the fetched sha is what the checkout reports.
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        assert len(checkout.head_sha) == 40


async def test_too_many_changed_lines_is_refused_before_any_disk_write(
    workspace, git_remote, tmp_path
):
    with pytest.raises(PullRequestTooLarge, match="max_changed_lines"):
        async with workspace.checkout(
            facts(git_remote, additions=9000), **CAPS
        ):
            pass
    assert not (tmp_path / "cache").exists()


async def test_too_many_changed_files_is_refused_before_any_disk_write(
    workspace, git_remote, tmp_path
):
    with pytest.raises(PullRequestTooLarge, match="max_changed_files"):
        async with workspace.checkout(
            facts(git_remote, changed_files=9000), **CAPS
        ):
            pass
    assert not (tmp_path / "cache").exists()


async def test_teardown_removes_the_worktree_and_the_run_ref(
    workspace, git_remote, tmp_path
):
    async with workspace.checkout(facts(git_remote), **CAPS) as checkout:
        path = checkout.path
    assert not path.exists()
    assert await workspace.run_refs() == []


async def test_a_repeated_run_returns_to_the_same_baseline(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    first = sorted(p.name for p in (workspace.cache_dir / "runs").iterdir())
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    assert sorted(p.name for p in (workspace.cache_dir / "runs").iterdir()) == first
    assert await workspace.run_refs() == []


async def test_two_concurrent_checkouts_do_not_interfere(workspace, git_remote):
    async def once(number: int) -> str:
        async with workspace.checkout(
            facts(git_remote, number=number), **CAPS
        ) as checkout:
            await asyncio.sleep(0)
            return str(checkout.path)

    first, second = await asyncio.gather(once(7), once(7))
    assert first != second


async def test_no_credential_reaches_the_wire(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    sent = {key.lower() for request in git_remote.requests for key in request}
    assert "authorization" not in sent
    assert "proxy-authorization" not in sent


async def test_the_mirror_keeps_no_remote_url(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    assert "127.0.0.1" not in (workspace.mirror / "config").read_text()
```

Both concurrent checkouts use pull request 7 because the fixture serves one; they must still get distinct run directories, which is what the assertion pins.

- [ ] **Step 2: Run to verify they fail**

Run: `poetry run pytest tests/test_workspace.py -q`
Expected: FAIL — `ImportError: cannot import name 'Workspace'`.

- [ ] **Step 3: Implement `repo.py`**

```python
"""One bare mirror per repository, a detached worktree per run.

The mirror carries the full commit graph, so ``git merge-base`` is always
answerable and the second review of the day fetches almost nothing. Two
concurrent runs are two worktrees over one object store, which is git's
designed use -- the run directories are **siblings** of the mirror, because
``$GIT_DIR/worktrees`` is where git keeps each worktree's own administrative
files.

The only shared mutable state is the mirror's ref namespace, and one
``asyncio.Lock`` serialises every write to it: the fetch and the teardown's
ref deletion alike, since ``update-ref -d`` and a concurrent fetch contend
for ``packed-refs.lock``. One process-wide lock is enough because the daemon
is one process.

The diff is computed **in the bare mirror**, not in the worktree. ``git
diff`` honours the ``.gitattributes`` of the tree it runs in, so a pull
request that adds ``*.py -diff`` would render its own changes as "Binary
files differ" while the API's addition count looked normal -- a
content-hiding attack on the review that needs no execution at all.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from .gitcmd import WorkspaceError, run_git

logger = logging.getLogger(__name__)

GITHUB_BASE = "https://github.com"

#: Run-scoped refs live under here, so the startup sweep can recognise one
#: left behind by a crash.
RUN_REF_PREFIX = "refs/run"


@dataclass(frozen=True)
class PullRequestFacts:
    """What the checkout needs to know before it touches the disk."""

    number: int
    head_sha: str
    base_ref: str
    additions: int
    deletions: int
    changed_files: int

    @property
    def changed_lines(self) -> int:
        """Added plus deleted: what the line cap is measured against."""
        return self.additions + self.deletions


@dataclass(frozen=True)
class Checkout:
    """An untrusted tree on disk, and the diff that describes it."""

    path: Path
    head_sha: str
    merge_base: str
    diff: str


class PullRequestTooLarge(WorkspaceError):
    """A size cap fired, before anything was written to disk."""

    def __init__(self, cap: str, observed: int, limit: int) -> None:
        self.cap = cap
        self.observed = observed
        self.limit = limit
        super().__init__(f"{cap}: {observed} exceeds the configured {limit}")


class Workspace:
    """The checkout cache for one repository."""

    def __init__(
        self, repo: str, cache_dir: Path, base_url: str = GITHUB_BASE
    ) -> None:
        self.repo = repo
        self.cache_dir = Path(cache_dir)
        # Not a safety knob: the https-only whitelist applies whatever this
        # is, and a GitHub Enterprise host is a real deployment. It is the
        # same shape as the client's configurable API base.
        self.base_url = base_url
        self._lock = asyncio.Lock()

    @property
    def mirror(self) -> Path:
        """The bare mirror for this repository."""
        return self.cache_dir / f"{self.repo.replace('/', '__')}.git"

    @property
    def runs(self) -> Path:
        """Where per-run worktrees live, beside the mirror rather than in it."""
        return self.cache_dir / "runs"

    @property
    def remote_url(self) -> str:
        """The anonymous https URL the mirror fetches from."""
        return f"{self.base_url.rstrip('/')}/{self.repo}.git"

    async def run_refs(self) -> list[str]:
        """Every run-scoped ref currently in the mirror."""
        if not self.mirror.exists():
            return []
        out = await run_git(
            "-C", str(self.mirror), "for-each-ref", "--format=%(refname)",
            RUN_REF_PREFIX,
        )
        return out.split()

    async def sweep(self) -> None:
        """Clear what a crashed run left behind.

        A crash between ``worktree add`` and teardown leaves a worktree and a
        run-scoped ref forever. Startup is the one moment when no git of ours
        is running, so it is also the only safe moment to remove a stale lock
        file -- mid-flight, the lock might belong to a live process.
        """
        if not self.mirror.exists():
            return
        for lock in self.mirror.rglob("*.lock"):
            lock.unlink(missing_ok=True)
            logger.warning("removed a stale git lock: %s", lock)
        await run_git("-C", str(self.mirror), "worktree", "prune")
        for ref in await self.run_refs():
            await run_git("-C", str(self.mirror), "update-ref", "-d", ref)
        if self.runs.exists():
            shutil.rmtree(self.runs, ignore_errors=True)

    @asynccontextmanager
    async def checkout(
        self,
        facts: PullRequestFacts,
        *,
        max_changed_files: int,
        max_changed_lines: int,
    ) -> AsyncIterator[Checkout]:
        """Check the pull request head out, and take it away afterwards.

        The caps are arguments rather than state because ``budget`` is
        reloaded on ``SIGHUP``: a workspace holding a snapshot would silently
        ignore a tightened cap, which is the failure the reload exists to
        prevent.
        """
        self._gate(facts, max_changed_files, max_changed_lines)

        run_id = uuid.uuid4().hex[:12]
        ref = f"{RUN_REF_PREFIX}/{run_id}"
        run_path = self.runs / run_id

        head_sha = await self._fetch(facts, ref)
        merge_base = await run_git(
            "-C", str(self.mirror), "merge-base",
            f"refs/heads/{facts.base_ref}", head_sha,
        )
        merge_base = merge_base.strip()
        diff = await run_git(
            "-C", str(self.mirror), "diff", "--no-ext-diff", merge_base, head_sha
        )
        self.runs.mkdir(parents=True, exist_ok=True)
        await run_git(
            "-C", str(self.mirror), "worktree", "add", "--detach",
            str(run_path), head_sha,
        )
        try:
            yield Checkout(
                path=run_path, head_sha=head_sha, merge_base=merge_base, diff=diff
            )
        finally:
            await self._teardown(run_path, ref)

    @staticmethod
    def _gate(
        facts: PullRequestFacts, max_changed_files: int, max_changed_lines: int
    ) -> None:
        """Refuse an oversized pull request before the first git invocation."""
        if facts.changed_files > max_changed_files:
            raise PullRequestTooLarge(
                "max_changed_files", facts.changed_files, max_changed_files
            )
        if facts.changed_lines > max_changed_lines:
            raise PullRequestTooLarge(
                "max_changed_lines", facts.changed_lines, max_changed_lines
            )

    async def _fetch(self, facts: PullRequestFacts, ref: str) -> str:
        """Fetch the head and the base branch, and return the fetched sha.

        Both refspecs are forced. Without the ``+`` on the base branch, a
        force-push upstream makes the fetch fail non-fast-forward, and every
        review of every pull request on that base fails until an operator
        intervenes.
        """
        async with self._lock:
            if not self.mirror.exists():
                self.mirror.parent.mkdir(parents=True, exist_ok=True)
                await run_git("init", "--bare", "-q", str(self.mirror))
                self.cache_dir.chmod(0o700)
            await run_git(
                "-C", str(self.mirror), "fetch", "--no-tags",
                "--no-recurse-submodules", self.remote_url,
                f"+refs/pull/{facts.number}/head:{ref}",
                f"+refs/heads/{facts.base_ref}:refs/heads/{facts.base_ref}",
            )
            # The fetched sha, not the API's: the head can move between the
            # two reads, and a review has to name the commit it read.
            out = await run_git("-C", str(self.mirror), "rev-parse", ref)
        return out.strip()

    async def _teardown(self, run_path: Path, ref: str) -> None:
        """Best-effort: a leak the operator is told about beats a hidden one."""
        try:
            await run_git(
                "-C", str(self.mirror), "worktree", "remove", "--force", str(run_path)
            )
            async with self._lock:
                await run_git("-C", str(self.mirror), "update-ref", "-d", ref)
        except WorkspaceError:
            logger.warning("could not tear down %s; it is leaking disk", run_path)
```

- [ ] **Step 4: Run to verify they pass**

Run: `poetry run pytest tests/test_workspace.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pr_review_agent/workspace tests/test_workspace.py
git commit -m "Check a pull request head out into an isolated worktree"
```

---

### Task 6: The startup sweep, tested against a simulated crash

**Files:**
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: `Workspace.sweep` (Task 5).
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_sweep_clears_what_a_crash_left_behind(workspace, git_remote):
    # Simulate a crash between `worktree add` and teardown: enter the context
    # manager by hand and never exit it.
    manager = workspace.checkout(facts(git_remote), **CAPS)
    checkout = await manager.__aenter__()
    assert checkout.path.exists()
    assert await workspace.run_refs() != []

    await workspace.sweep()

    assert not checkout.path.exists()
    assert await workspace.run_refs() == []


async def test_the_sweep_removes_a_stale_lock(workspace, git_remote):
    async with workspace.checkout(facts(git_remote), **CAPS):
        pass
    stale = workspace.mirror / "refs" / "stale.lock"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("")

    await workspace.sweep()

    assert not stale.exists()


async def test_the_sweep_is_a_no_op_before_the_first_fetch(workspace):
    await workspace.sweep()  # must not raise
```

- [ ] **Step 2: Run**

Run: `poetry run pytest tests/test_workspace.py -q`
Expected: PASS if Task 5's `sweep` is right; otherwise fix `sweep`, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_workspace.py
git commit -m "Sweep a crashed run's leftovers at startup"
```

---

### Task 7: The four escapes that must not fire

**Files:**
- Create: `tests/test_workspace_safety.py`

**Interfaces:**
- Consumes: `Workspace`, `git_remote`, the `git` helper from `conftest.py`.
- Produces: nothing.

Each test was chosen because it **fails when its mitigation is removed**. The two it replaces did not: a hostile `GIT_CONFIG_GLOBAL` planted in `os.environ` never reaches a child whose environment is built explicitly, and `worktree add` never populates submodules, so an `ext::` gitlink test passes with every mitigation gone.

- [ ] **Step 1: Write the tests**

```python
"""Escapes that are made available and must not fire.

Every test here plants a real hazard and asserts the checkout is unmoved.
Removing the corresponding mitigation must make the test fail -- a safety
test that passes either way pins nothing.
"""

import os
import subprocess
import sys

import pytest

from pr_review_agent.workspace import Workspace
from pr_review_agent.workspace.gitcmd import GitCommandError, run_git

from .conftest import PR_NUMBER, git

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the daemon is deployed on POSIX hosts"
)

CAPS = {"max_changed_files": 100, "max_changed_lines": 5000}


async def test_a_hostile_home_gitconfig_never_fires(
    workspace, git_remote, tmp_path, monkeypatch
):
    """`core.hooksPath`, a smudge filter, `fsmonitor` and `diff.external`.

    All four live in the user's gitconfig, which is why config nulling
    rather than a flag per mechanism is the control. `HOME` is passed
    through to the child, so this is planted where git really looks.
    """
    home = tmp_path / "home"
    home.mkdir()
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "fired"
    for name in ("post-checkout", "reference-transaction", "post-index-change"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    evil = tmp_path / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    evil.chmod(0o755)
    (home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {evil}\n"
        f"[filter \"evil\"]\n\tsmudge = {evil}\n"
        f"[diff]\n\texternal = {evil}\n"
    )
    monkeypatch.setenv("HOME", str(home))

    async with workspace.checkout(_facts(git_remote), **CAPS):
        pass

    assert not marker.exists(), "a hostile gitconfig fired"


async def test_a_symlink_checks_out_as_a_regular_file(
    workspace, git_remote, tmp_path
):
    """The exfiltration path: `AGENTS.md -> ~/.claude/.credentials.json`.

    The reviewer reads the tree and can quote what it read into a public
    comment, so a live symlink out of the worktree is a leak that needs no
    execution at all.
    """
    secret = tmp_path / "hostsecret"
    secret.write_text("SUPER SECRET\n")
    _add_to_pr_head(git_remote, lambda repo: os.symlink(secret, repo / "AGENTS.md"))

    async with workspace.checkout(_facts(git_remote), **CAPS) as checkout:
        link = checkout.path / "AGENTS.md"
        assert not link.is_symlink()
        assert "SUPER SECRET" not in link.read_text()
        assert str(secret) in link.read_text()


async def test_a_gitattributes_cannot_blank_the_diff(workspace, git_remote):
    """`*.py -diff` would render the PR's own changes as "Binary files differ".

    The reviewer would see nothing while the API's addition count looked
    normal. Computing the diff in the bare mirror is what stops it.
    """
    def add(repo):
        (repo / ".gitattributes").write_text("*.py -diff\n")

    _add_to_pr_head(git_remote, add)

    async with workspace.checkout(_facts(git_remote), **CAPS) as checkout:
        assert "def added():" in checkout.diff
        assert "Binary files" not in checkout.diff


async def test_only_https_is_allowed(tmp_path, git_remote, monkeypatch):
    """`GIT_ALLOW_PROTOCOL` is the control, and it is a constant.

    A `file://` remote is what the fixtures would have used; it is refused,
    which is exactly why the double speaks https.
    """
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    for url in (f"file://{git_remote.serve_root}", git_remote.url.replace("https", "http")):
        with pytest.raises(GitCommandError, match="not allowed"):
            await run_git("ls-remote", url, cwd=tmp_path)
    # The https double, by contrast, answers.
    assert git_remote.head_sha in await run_git(
        "ls-remote", git_remote.url, cwd=tmp_path
    )
```

with the two helpers:

```python
def _facts(remote):
    from pr_review_agent.workspace import PullRequestFacts

    return PullRequestFacts(
        number=PR_NUMBER,
        head_sha=remote.head_sha,
        base_ref=remote.base_ref,
        additions=2,
        deletions=0,
        changed_files=1,
    )


def _add_to_pr_head(remote, mutate):
    """Rewrite the served `refs/pull/N/head` with one more commit.

    The double serves a bare repository, so the change is made in a scratch
    clone and pushed back into the served ref.
    """
    work = remote.serve_root.parent / "mutate"
    if work.exists():
        subprocess.run(["rm", "-rf", str(work)], check=True)
    git("clone", "-q", str(remote.serve_root), str(work))
    git("checkout", "-q", remote.head_sha, cwd=work)
    mutate(work)
    git("add", "-A", cwd=work)
    git("commit", "-qm", "hostile", cwd=work)
    new_head = git("rev-parse", "HEAD", cwd=work)
    git("update-ref", f"refs/pull/{PR_NUMBER}/head", new_head, cwd=remote.serve_root)
    remote.head_sha = new_head
```

`GitRemote` must therefore be a mutable dataclass (it is — no `frozen=True`), and the `workspace` fixture from `tests/test_workspace.py` needs to be importable; move it into `conftest.py` if it is not already there.

- [ ] **Step 2: Run**

Run: `poetry run pytest tests/test_workspace_safety.py -q`
Expected: PASS.

- [ ] **Step 3: Prove each test can fail**

For each mitigation, remove it, run the matching test, confirm FAIL, restore it. Record the four results in the commit message. A safety test that passes with its mitigation gone is worse than no test.

| Remove | Expect to fail |
| :-- | :-- |
| `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` from `git_environment` | `test_a_hostile_home_gitconfig_never_fires` |
| `-c core.symlinks=false` | `test_a_symlink_checks_out_as_a_regular_file` |
| the mirror `-C` on the diff (run it in the worktree) | `test_a_gitattributes_cannot_blank_the_diff` |
| `GIT_ALLOW_PROTOCOL` from `git_environment` | `test_only_https_is_allowed` |

- [ ] **Step 4: Commit**

```bash
git add tests/test_workspace_safety.py
git commit -m "Pin the four escapes the checkout must not allow"
```

---

### Task 8: Bootstrap — the git version and the fetch route

**Files:**
- Modify: `src/pr_review_agent/bootstrap.py`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `git_version`, `run_git`, `MINIMUM_GIT_VERSION` (Task 3).
- Produces: `async check_git(repo: str, base_url: str = GITHUB_BASE) -> list[CheckResult]`, appended to whatever `bootstrap.main` already reports.

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_git_version_floor_is_checked():
    results = await check_git("owner/name", base_url="https://127.0.0.1:1")
    names = [result.name for result in results]
    assert "git version" in names


async def test_an_unreachable_remote_is_reported_not_raised():
    results = await check_git("owner/name", base_url="https://127.0.0.1:1")
    route = next(r for r in results if r.name == "git fetch route")
    assert route.ok is False
    assert route.detail


async def test_a_reachable_remote_passes(git_remote, monkeypatch):
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    base = git_remote.url.rsplit("/owner/name.git", 1)[0]
    results = await check_git("owner/name", base_url=base)
    route = next(r for r in results if r.name == "git fetch route")
    assert route.ok is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `poetry run pytest tests/test_bootstrap.py -q`
Expected: FAIL — `ImportError: cannot import name 'check_git'`.

- [ ] **Step 3: Implement**

```python
async def check_git(
    repo: str, base_url: str = GITHUB_BASE
) -> list[CheckResult]:
    """Is git new enough, and does the fetch route work?

    The version comes first because it is what makes the rest of the
    hardening real: below 2.32, ``GIT_CONFIG_GLOBAL`` is ignored without an
    error, so the control that neutralises the host's gitconfig is simply
    absent. The route is a second question from the poller's: `github.com`
    and `api.github.com` are different hosts and, on an allowlist firewall,
    different rules.
    """
    results = []
    try:
        version = await git_version()
        ok = version >= MINIMUM_GIT_VERSION
        wanted = ".".join(str(part) for part in MINIMUM_GIT_VERSION)
        found = ".".join(str(part) for part in version)
        results.append(
            CheckResult(
                "git version",
                ok,
                f"found {found}, need at least {wanted}",
            )
        )
    except WorkspaceError as exc:
        results.append(CheckResult("git version", False, str(exc)))
        return results

    url = f"{base_url.rstrip('/')}/{repo}.git"
    try:
        await run_git("ls-remote", "--heads", url, timeout=30.0)
        results.append(CheckResult("git fetch route", True, f"{url} answers"))
    except WorkspaceError as exc:
        results.append(CheckResult("git fetch route", False, str(exc)))
    return results
```

Wire it into `bootstrap.main` the way the existing checks are wired, and extend the module docstring with a fourth paragraph:

```
**Git can fetch.** The review needs the code on disk, which is a different
host from the API -- `github.com`, not `api.github.com` -- and on an
allowlist-based firewall a different rule. The git version is checked first,
because `GIT_CONFIG_GLOBAL` is ignored without error below 2.32 and the
checkout's whole hardening rests on it.
```

- [ ] **Step 4: Run**

Run: `poetry run pytest tests/test_bootstrap.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pr_review_agent/bootstrap.py tests/test_bootstrap.py
git commit -m "Check the git version and the fetch route at bootstrap"
```

---

### Task 9: Documentation

**Files:**
- Create: `docs/WORKSPACE.md`
- Modify: `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/BUDGET.md`, `docs/CONFIG.md`, `docs/DESIGN.md`, `DEVELOPER.md`, `README.md`

**Interfaces:** none.

- [ ] **Step 1: Write `docs/WORKSPACE.md`**

Sections, following the house style of `QUEUE.md` (short, reasoned, one idea per heading):

- *What it does* — code on disk at an exact commit, a merge-base diff, nothing executed, cleaned up.
- *Why a checkout rather than the API diff* — truncation, rate limit, context.
- *One mirror, a worktree per run* — including why the runs are siblings of the mirror.
- *The tree is untrusted* — the environment is the control; the table of `-c` flags; why `core.symlinks=false` is a leak fix and not an execution fix.
- *The diff is computed in the mirror* — the `*.py -diff` attack.
- *The caps are layer 2* — with a pointer to `BUDGET.md`, and the honest statement that they bound what the engine reads, not the disk.
- *The startup sweep* — what a crash leaves and why startup is the only safe moment to clear a lock.
- *What the workspace does not do* — no review, no prompt, no publish; no `head_sha` write-back to the queue row.

- [ ] **Step 2: Update the status tables**

- `ARCHITECTURE.md`: a component row between "Queue and lease" and "Budget governor" — `| 4.5 | **Workspace** | Fetch and check out a pull request head, diff it, tear it down. | implemented — [WORKSPACE.md](WORKSPACE.md) |` — plus the `workspace/` entries in the package-layout block and a line in the layering section recording that `poller/pulls.py` imports `workspace`, never the reverse.
- `ROADMAP.md`: a "Workspace (fetch, checkout, diff)" row in the state table; the "Next" list loses the checkout prerequisite from the engine-adapter item.
- `BUDGET.md`: layer 2's row becomes `| 2 | Path exclusions, diff-size caps, pre-flight token estimate | diff-size caps **done** — [WORKSPACE.md](WORKSPACE.md); the rest with the engine adapter |`, and the paragraph below it gains a sentence saying the caps landed early because the checkout is the first thing that needs them, and that they bound what the engine reads rather than the disk.
- `CONFIG.md`: a `workspace` section table; the two new `budget` keys; change "the one exception" to "the two exceptions" in the `store` paragraph; note that the caps reload on `SIGHUP` with the rest of `budget` and `cache_dir` does not.
- `DESIGN.md`: prerequisite 1 gains the git binary (≥ 2.32) beside the egress list.
- `DEVELOPER.md`: `git` ≥ 2.32 as a prerequisite, and `cryptography` in the dev-dependency list with one line saying it generates the test fixture's certificate.
- `README.md`: the status table row.

- [ ] **Step 3: Commit**

```bash
git add docs DEVELOPER.md README.md
git commit -m "Document the workspace checkout"
```

---

### Task 10: The full gate, and the pull request

- [ ] **Step 1: Run the whole gate**

```bash
poetry run pytest
poetry run ruff check .
poetry run ruff format --check .
poetry run pylint src --rcfile=.pylintrc --fail-under=9.0
poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 --disable=missing-function-docstring,missing-module-docstring
poetry run pyright src tests
```

Quote every result. Do not claim done on a prediction.

- [ ] **Step 2: Push and open the pull request**

```bash
git push -u origin workspace-checkout
```

The body states, per `CLAUDE.md` §5: that this widens nothing about what triggers a review; that it adds two spending caps whose defaults are pinned by a test; that it adds no trust check; and that the checked-out tree is untrusted input which the subsystem treats as data.

## Self-Review

**Spec coverage.** Every spec section maps to a task: strategy and layout → 5; layering and `PullRequestFacts` → 2, 5; the ordered steps → 5; the mirror diff → 5, 7; the startup sweep → 5, 6; the hardened runner → 3, 7; anonymous fetch → 5 (`test_no_credential_reaches_the_wire`); the caps → 1, 5; the `workspace` section → 1; errors → 3, 5; bootstrap → 8; the tests table → 4, 5, 6, 7; docs → 9.

**Placeholders.** None: every step carries the code it needs.

**Type consistency.** `PullRequestFacts` is defined once in Task 5 and consumed with the same field names in Tasks 2, 5, 7. `run_git` keeps the signature `(*args, cwd=None, timeout=...)` in Tasks 3, 5, 8. `CheckResult(name, ok, detail)` matches `bootstrap.py`'s existing dataclass — verify that before writing Task 8 and adjust if it differs.

**Known ordering wrinkle.** Task 2 imports `PullRequestFacts` from Task 5's module. Implement Task 3, then 5, then 2 if working strictly by dependency; the numbering follows the reading order of the spec, not the build order.
