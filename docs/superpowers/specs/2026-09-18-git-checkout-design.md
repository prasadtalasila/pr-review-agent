# Git checkout — design

Status: approved 2026-09-18. Implements [issue #11](https://github.com/prasadtalasila/pr-review-agent/issues/11),
the prerequisite for the engine adapter.

Revised the same day after an independent review that verified each claim
against git 2.43. Three findings changed the design rather than polishing it:
symlinks, the protocol whitelist, and two safety tests that could not fail.
They are marked **[review]** where they land.

Revised again to serve the test fixtures over **https** rather than `file://`.
That removed the last test-shaped knob from production code and made "no
credential reaches the wire" an assertion rather than a claim.

## 🎯 Goal

Put a pull request's code **on disk at an exact commit**, with a merge-base
diff, without executing any of it and without letting it read anything else,
and take it away again — so the engine adapter has something to review.

Nothing in the agent can fetch anything today. `GitHubClient` has one method, a
conditional `get()`, and `RepoEndpoints` builds only the three repo-wide
polling paths.

The subsystem **stops at the checkout**. It runs no review, builds no prompt,
posts nothing and spends no tokens. Like the queue and the governor before it,
it lands with no caller: the worker that will use it does not exist yet.

## 📐 Scope

In:

- `src/pr_review_agent/workspace/` — `gitcmd.py`, `repo.py`, `__init__.py`.
- `poller/endpoints.py` and `poller/payloads.py` — the single-pull-request
  path and its mapping, which is where `head_sha` gets resolved for a mention.
- `config.py` — two cap keys in `budget`, and a new optional `workspace`
  section holding only `cache_dir`.
- Two added checks in `bootstrap.py`: the git version, and that the fetch
  route works.
- One dev-only dependency for certificate generation in the test fixture.
- `docs/WORKSPACE.md`; the layer-2 row in `BUDGET.md`; status rows in
  `ARCHITECTURE.md`, `ROADMAP.md`, `CONFIG.md`, `README.md` and
  `config.example.yaml`.

Out:

- Reviewing, prompting, publishing — the engine adapter and publisher own
  those.
- Writing the resolved `head_sha` back to the queue row. The worker that
  claims the row is what knows the row; this returns the value.
- Sandboxing the *engine* (containers, seccomp, a read-only tool set). This
  issue guarantees the checkout executes nothing and that the tree cannot
  reach outside itself; `DESIGN.md` already specifies the read-only tool set
  for the review step.
- Layer 2's other two parts, path exclusions and the pre-flight token
  estimate. Both need a diff in hand to be worth anything, and the estimate
  needs the engine's tokeniser.
- A byte-denominated disk bound. See "what the caps do not bound" below.

## 🚫 Why not the API diff

`GET /pulls/{n}/files` paginates and truncates on a large pull request;
`git diff` does not. A local diff costs no request, so it does not compete
with the poller for the rate-limit budget `POLLER.md` depends on. And a
reviewer needs surrounding code, sibling files and `AGENTS.md` — context that
only exists in a checkout. `DESIGN.md` already lists `github.com` and
`codeload.github.com` among the egress prerequisites: the design assumed a
checkout and never wrote it up.

## 🧩 Strategy: one bare mirror, a worktree per run

```text
cache_dir/
├── <owner>__<name>.git/        one bare mirror, incrementally fetched
└── runs/<run-id>/              per-run worktree, detached, removed on teardown
```

**[review]** The run directories are siblings of the mirror, not children of
it. `$GIT_DIR/worktrees/` is where git keeps each worktree's own `HEAD`,
`index` and `commondir`; putting a working tree at that path produces one
directory serving both roles, and `git status` inside it reports git's
administrative files as untracked.

The mirror carries the full commit graph, so `git merge-base` is always
answerable. The second review of the day fetches almost nothing. Two
concurrent runs are two worktrees over one object store, which is git's
designed use.

The one piece of shared mutable state is the mirror's ref namespace. An
in-process `asyncio.Lock` serialises **every write to it** — the fetch and the
teardown's ref deletion alike, since `update-ref -d` and a concurrent fetch
contend for `packed-refs.lock`. That one lock is sufficient, not merely
convenient, because the daemon is a single process: the same assumption the
queue's per-PR lease already rests on.

### Rejected: per-run shallow clone

Total isolation and `rmtree` teardown, but every run re-downloads, and
"deepen until the merge base appears" is an unbounded loop against history we
do not control. It stalls worst on exactly the pull requests that branched
long ago.

### Rejected: codeload tarballs diffed in Python

The smallest untrusted-input surface — a tarball carries no hooks, submodules
or filters. But there is no merge base without an extra compare request,
`difflib` has no rename detection or binary handling, every run downloads two
full trees, and tar extraction brings its own path-traversal hazard.

## 🔌 Layering: no HTTP inside `workspace/`

The size gate needs `additions`, `deletions` and `changed_files`; the mention
trigger needs `head_sha`. All four come from one
`GET /repos/{owner}/{name}/pulls/{n}`.

Rather than let `workspace/` reach for the client, the facts arrive as a value
object:

```python
@dataclass(frozen=True)
class PullRequestFacts:
    number: int
    head_sha: str
    base_ref: str
    additions: int
    deletions: int
    changed_files: int
```

The request and its mapping live in `poller/`, the declared seam where every
quirk of the GitHub REST shape is resolved. `workspace/` is then pure git plus
filesystem, and its whole suite runs with no HTTP — the property that keeps
the trigger suite free of the network, applied again.

`payloads.py` currently says `head_sha` is "left unresolved and read when the
trigger is claimed". This is the code that reads it, so that docstring is
updated to point here.

## 🔁 One checkout, in order

```python
facts = await pull_request_facts(client, endpoints, n)   # poller side, 1 request
async with workspace.checkout(                           # workspace side, no HTTP
    facts,
    max_changed_files=config.budget.max_changed_files,
    max_changed_lines=config.budget.max_changed_lines,
) as co:
    co.path        # worktree root, detached at co.head_sha
    co.head_sha    # the sha actually checked out
    co.merge_base
    co.diff        # computed in the mirror, not in the worktree
```

1. **Size gate, before the first git invocation.** `additions + deletions`
   against `max_changed_lines`, `changed_files` against `max_changed_files`;
   `PullRequestTooLarge` otherwise. Raising here is what makes "refused before
   anything is written to disk" literally true rather than approximately true.
2. Ensure the mirror exists (`git init --bare` on first use), under the lock.
3. Fetch `+refs/pull/{n}/head:refs/run/{run-id}` and
   `+refs/heads/{base_ref}:refs/heads/{base_ref}`, `--no-tags
   --no-recurse-submodules`, under the lock. **[review]** Both refspecs are
   forced: without the `+`, a force-push to the base branch makes the fetch
   fail non-fast-forward, and *every* review of *every* pull request on that
   base then fails until an operator intervenes.
4. Read the fetched sha. **A fork is not a special case**: `refs/pull/{n}/head`
   lives in the base repository for forks and branches alike, so one code path
   satisfies both halves of that acceptance item.
5. `git merge-base <base_tip> <head_sha>`. With no common ancestor it exits 1
   with empty stdout, surfacing as `GitCommandError`. GitHub will not open
   such a pull request, so this is a corruption path rather than a
   legitimate one — it is named here so it is not mistaken for a bug later.
6. `git worktree add --detach <cache_dir>/runs/<run-id> <head_sha>`, outside
   the lock.
7. Teardown in `finally`: `worktree remove --force`, then `update-ref -d` on
   the run-scoped ref **under the lock**.

**The fetched sha, not the API's, is what gets checked out and reported.** The
head can move between the two reads, and a review has to name the commit it
actually read. The publisher re-checks the live head before posting regardless
— that check belongs to publish time, as `QUEUE.md` sets out.

### The diff is computed in the mirror

**[review]** `git diff` honours the `.gitattributes` of the tree it runs in. A
pull request that adds `*.py -diff` makes its own Python changes render as
`Binary files … differ`, with `--numstat` reporting `-  -`: the reviewer sees
nothing, while the API's `additions` count looks perfectly normal. That is a
content-hiding attack on the review itself, and it needs no execution at all.

So the diff runs in the **bare mirror** — `git -C <mirror> diff --no-ext-diff
<merge_base> <head_sha>` — which does not read in-tree attributes. The
worktree still exists for the engine to read surrounding files; it is simply
not where the diff is produced.

### Startup sweep

**[review]** A crash between steps 6 and 7 leaves a worktree and a
`refs/run/*` ref behind forever. At startup, before the first checkout, the
workspace prunes stale worktrees, deletes any leftover `refs/run/*`, and
clears stale `*.lock` files in the mirror. Startup is the one moment when no
git of ours is running, so clearing a lock there is safe in a way that
clearing it mid-flight would not be.

## 🛡 The hardened runner

Every `git` invocation goes through one function in `gitcmd.py`. Nothing else
in the package shells out, so the hardening cannot be forgotten at a call
site.

**The environment is the control.** It is built explicitly rather than
inherited:

| Variable | Why |
| :-- | :-- |
| `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null` | The host's gitconfig is where `core.hooksPath`, `core.fsmonitor`, `diff.external`, credential helpers and smudge filters all live. Neutralising it disables every one of them at once. Requires **git ≥ 2.32**; on older git the variables are ignored silently, which is why the version is checked at startup. |
| `GIT_ALLOW_PROTOCOL=https` | **[review]** A whitelist that overrides all `protocol.*` config. The `-c protocol.allow=never` approach does *not* survive a specific `protocol.ext.allow=always`, and an `ext::` submodule URL is the shortest path from "checked out untrusted code" to "ran it". A hard-coded constant, with no way for a caller or an operator to widen it. |
| `GIT_TERMINAL_PROMPT=0` | An unreadable repository fails in a second instead of blocking the daemon on a password prompt. |
| `PATH`, `HOME`, `https_proxy`, `no_proxy`, `GIT_SSL_CAINFO` | Passed through. `PATH` is needed to find `git-remote-https`; the proxy and CA variables are exactly what the allowlist-firewall hosts `DESIGN.md` worries about depend on, since a TLS-inspecting proxy presents its own certificate. `HOME` is safe to pass because `GIT_CONFIG_GLOBAL` overrides `$HOME/.gitconfig` — and passing it is what lets the safety tests plant a hostile config where git would really look. |

**Per-invocation `-c` flags** are redundancy, not the primary control, except
for the first two, which stop things config-nulling does not:

| Flag | What it stops |
| :-- | :-- |
| `core.symlinks=false` | **[review]** A symlink in the tree resolving outside it. `AGENTS.md → ~/.claude/.credentials.json` checks out as a live symlink; the reviewer reads it and can quote it into a public comment. With this flag git writes a plain file containing the target path. Nothing downstream can undo a symlink, so this is the checkout's decision to make. |
| `transfer.fsckObjects=true` | **[review]** A malicious pack — a tree containing `.GIT/`, a malformed `.gitmodules` — is rejected at `index-pack`, at the boundary, rather than at checkout. |
| `core.hooksPath=/dev/null` | A hook planted in the mirror's own `hooks/`. Verified to work independently of config nulling. |
| `submodule.recurse=false` | Recursing into a submodule. Belt only: `worktree add` never populates submodules. |
| `filter.lfs.smudge=`, `filter.lfs.process=`, `filter.lfs.required=false` | A filter **named `lfs`** running during checkout. It is worth stating the narrowness: this trio does nothing about a filter named anything else, and config nulling is what actually covers those. |

`--no-ext-diff` on the diff; `stdin` is `DEVNULL`. `git submodule`, `git lfs`,
and anything resembling a build, install or test are never invoked.

**Timeouts terminate before they kill.** **[review]** `Process.kill()` is
SIGKILL, and git cleans up its `.lock` files on SIGTERM but cannot on SIGKILL
— a killed fetch can leave `refs/run/y.lock` that makes every later fetch fail
until an operator removes it by hand. So each invocation gets `terminate()`
and a short grace period before `kill()`, and the startup sweep clears
whatever still gets left behind.

**Path traversal needs nothing added**: `.git`, `.GIT`, `git~1` and `.git.`
are all refused by git itself, with nothing written. The deployment target is
Linux with a case-sensitive filesystem, which this design assumes.

### The fetch is anonymous

No credential reaches git: not on the argv, not in the remote URL, not in
`.git/config`. The target repository is public, so the token the poller holds
buys nothing here, and not passing it is one fewer way to leak it to disk. A
private repository therefore fails the fetch loudly, which is the correct
outcome for a capability that has not been designed.

### Dropped after review

`GIT_ASKPASS=/bin/false` (redundant with `GIT_TERMINAL_PROMPT=0`, and that
path is not portable) and `GIT_LFS_SKIP_SMUDGE=1` (config nulling already
covers it). `worktree prune` moves out of teardown, where it is a no-op, into
the startup sweep, where it does real work.

## 💰 The caps are layer 2, so they live in `budget`

```yaml
budget:
  max_changed_files: 100
  max_changed_lines: 5000
```

`BUDGET.md`'s layer 2 is "path exclusions, diff-size caps, pre-flight token
estimate", scheduled "with the engine adapter". The diff-size cap arrives
here instead, because the checkout is the first thing that needs it. That row
becomes *partly done*.

Putting the keys in `budget` rather than in `workspace` keeps every spending
cap in one specification and one section, which is what `CLAUDE.md` §5 asks of
code that bounds spend. It has a second consequence worth having: **`budget`
is the only section `SIGHUP` hot-swaps**, so the caps can be tightened on a
running daemon.

That reload is why the caps are **passed per checkout** rather than captured
when the `Workspace` is constructed. A `Workspace` holding a snapshot would
silently ignore a reload — precisely the failure the reload mechanism exists
to prevent. Two integer parameters, not a new caps type: there is one caller
and one call site.

Both caps exist because neither bounds the other: two thousand one-line files
pass a line cap and still bury the engine; one fifty-thousand-line generated
file passes a file cap.

They are optional with defaults, unlike `session_tokens` and `weekly_tokens`,
which `config.py` deliberately refuses to default. The distinction is real: a
plan's token allowance is unpublished, so any default would be a fabricated
ceiling, whereas a diff-size cap is an ordinary engineering choice with a
defensible value. A test pins both defaults, so widening a cap is a
deliberate, visible diff.

### What the caps do not bound

**[review]** They bound **what the engine reads**, not the disk. The fetch
pulls every object reachable from the head, so a commit that adds a 100 MB
blob and a later one that deletes it reports `additions: 0` and still
downloads 100 MB. The earlier draft claimed the gate protected the volume;
that was wrong, and the issue's framing of it as a disk control is optimistic.

Disk is bounded in practice by GitHub's own push and repository limits, and by
the operator watching `cache_dir`. `fetch --filter=blob:limit=N` is the
git-native knob if a real byte bound is wanted later; it complicates checkout
and is out of scope for #11.

## ⚙️ The `workspace` section

```yaml
workspace:
  cache_dir: .cache/repos     # created 0700
```

Optional, holding a path and nothing else — the same shape as `store`, and
optional for the same reason: it cannot spend anything, because the cap that
can now lives in `budget`. `CONFIG.md`'s "the one exception" sentence becomes
"the two exceptions". Unknown keys inside it are rejected as everywhere else.

`cache_dir` is resolved and logged absolute at startup, as `store.path`
already is. It is **not** hot-swapped: moving the cache under a running daemon
would orphan the mirror, so a change is logged as needing a restart, like
`github`, `triggers` and `store`.

There is **no protocol setting**, at any layer. An earlier draft made the
allowed-protocol set a `Workspace` constructor argument so the tests could use
`file://` remotes; the tests now speak https to a local double instead, so the
whitelist is a constant and production has no widening knob for a test's
benefit.

## 🛑 Errors

| Error | Raised when |
| :-- | :-- |
| `WorkspaceError` | Base. |
| `PullRequestTooLarge` | A cap fired. Carries which cap and the observed numbers, in the style of the classifier's reason codes. |
| `GitCommandError` | Non-zero exit or timeout. Carries argv, exit code and a stderr tail. |

Teardown is best-effort inside `finally`: a failure logs at `WARNING` and does
not mask the original exception. A checkout that cannot be removed is a disk
leak, so the operator is told rather than the failure being swallowed.

## 🩺 Bootstrap

Two added checks: `git --version` against the 2.32 floor, and an anonymous
`ls-remote` against the configured repository. The version check comes first
because it is the one that makes the rest of the hardening real — below 2.32,
`GIT_CONFIG_GLOBAL` is ignored without error.

`DESIGN.md` lists `github.com` egress as a prerequisite to confirm, and the
poller's route answering says nothing about this one: they are different
hosts and, on an allowlist firewall, different rules.

## 🧪 What the tests must pin

### The remote is an HTTPS double, not a `file://` path

Fixtures build a real repository in `tmp_path` containing a commit reachable
**only** from `refs/pull/7/head` and from no branch — precisely the shape a
fork pull request has, so the fork criterion is met offline rather than
approximated.

It is served by `git http-backend` behind a `ThreadingHTTPServer` wrapped in
TLS with a generated certificate, bound to `127.0.0.1:0`. Tests reach it at
`https://127.0.0.1:<port>/owner/name.git`, with `GIT_SSL_CAINFO` pointing at
the fixture CA — a variable the runner passes through for production reasons
of its own.

Three things follow, and the third is why it is worth the fixture code:

1. **No test-only knob in production.** A `file://` remote is refused by
   `GIT_ALLOW_PROTOCOL=https` (verified: `fatal: transport 'file' not
   allowed`), which is what forced the earlier constructor argument. Speaking
   https removes the argument entirely.
2. **The tests exercise the production transport** — `git-remote-https` and
   real smart-HTTP — rather than a local path that skips it.
3. **The wire is observable.** The double records every request, so "no
   credential reaches git" stops being a claim about argv and becomes an
   assertion about what was actually sent.

Loopback is not network access: nothing leaves the host, and no test needs
egress. The cost is honest — roughly eighty lines of fixture and one dev
dependency (`trustme`, or `cryptography` directly) for certificate
generation.

Behaviour:

- a checkout is detached at the exact sha, for the branch case and the
  fork-shaped case;
- `head_sha` is resolved for a mention trigger, whose payload carries none;
- the diff is computed against the merge base, not the base tip;
- a force-pushed base branch still fetches;
- two concurrent checkouts of different pull requests both succeed and neither
  sees the other's files.

The wire, now that it can be inspected:

- no request carries an `Authorization` or `Proxy-Authorization` header;
- the mirror's `config` holds no remote URL, so nothing about the remote
  persists to disk.

Bounds (`CLAUDE.md` §5):

- a pull request over either cap raises `PullRequestTooLarge` and leaves
  **nothing** new under `cache_dir` — asserted on the directory, not on a
  mock;
- the default caps are pinned by value;
- after teardown the worktree is gone and the run-scoped ref is deleted, and a
  second run over the same pull request returns the **worktree and ref counts**
  to their baseline. That is what the test measures, and the acceptance row is
  worded to match: it is not a byte measurement, because gc runs on its own
  schedule.

Safety — **[review]** the earlier draft had three tests, two of which could
not fail. Each of these was checked to fail when its mitigation is removed:

| Test | Would have been vacuous because |
| :-- | :-- |
| A hostile `.gitconfig` planted at `HOME`/`XDG_CONFIG_HOME` defining `core.hooksPath`, a smudge filter, `core.fsmonitor` and `diff.external` → none fire | The original planted `GIT_CONFIG_GLOBAL` in `os.environ`, which an explicitly-built child environment never passes on. Deleting the mitigation just removed the variable, and git read `$HOME/.gitconfig` anyway. |
| A symlink pointing outside the worktree checks out as a **regular file** containing the target path | New; the hazard was missed entirely. |
| A pull request adding `*.py -diff` still produces a textual diff containing the added line | New; the hazard was missed entirely. |
| The production environment refuses a `file://` and an `http://` remote, while the https double succeeds | The `ext::`-submodule test it replaces passes with *every* mitigation removed: `worktree add` never populates submodules, so nothing was ever pinned. This one fails the moment `GIT_ALLOW_PROTOCOL` is dropped. |

The gitlink case stays only as a plain behavioural assertion — a submodule
directory is left empty — with no claim that it pins a mitigation.

`git ≥ 2.32` becomes a documented prerequisite in `DEVELOPER.md` and
`DESIGN.md`, and these tests **fail** rather than skip when git is missing or
too old: a safety test that silently skips is a safety test that never runs,
and on a pre-2.32 host the hostile-config test failing loudly is exactly the
signal wanted.

## 🔀 Branch boundary

The governor has landed, so the parallel branch is now the engine phase.

| This branch | Engine adapter / publisher |
| :-- | :-- |
| `workspace/`, `poller/endpoints.py`, `poller/payloads.py`, `bootstrap.py` | `engine/`, `publisher/`, the worker that drains the queue |
| `config.py` — `workspace` section, two `budget` cap keys | `config.py` — `engine` and `publish` sections |
| `BUDGET.md` layer-2 row: diff-size caps | `BUDGET.md` layers 2 and 3: path exclusions, token estimate, per-run enforcement |

Shared files are `config.py`, `config.example.yaml`, `BUDGET.md` and the
status rows in `ROADMAP.md` / `ARCHITECTURE.md` / `README.md`. All are
append-style conflicts; no shared function is modified by both.

## ✅ Acceptance, mapped

| Issue criterion | Where it is met |
| :-- | :-- |
| Fetch and check out at an exact sha, fork or branch | Steps 3–6; one path, both cases |
| `head_sha` resolved for a mention | `pull_request_facts` |
| Diff against the merge base | Step 5, full commit graph in the mirror |
| Two concurrent runs do not interfere | Per-run worktrees; one lock over every ref write |
| Hooks, submodules, LFS disabled, pinned by tests | The hardened runner; the safety table above |
| Oversized pull request refused before disk | Step 1, asserted on `cache_dir` |
| Torn down; repeated runs do not grow disk | Step 7 and the startup sweep; baseline worktree- and ref-count test |
| No test requires network | A loopback TLS double serving `git http-backend`; nothing leaves the host |
