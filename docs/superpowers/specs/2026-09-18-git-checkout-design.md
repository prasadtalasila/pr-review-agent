# Git checkout — design

Status: approved 2026-09-18. Implements [issue #11](https://github.com/prasadtalasila/pr-review-agent/issues/11),
the prerequisite for the engine adapter.

## 🎯 Goal

Put a pull request's code **on disk at an exact commit**, with a merge-base
diff, without executing any of it, and take it away again — so the engine
adapter has something to review.

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
- One added check in `bootstrap.py`: the fetch route works.
- `docs/WORKSPACE.md`; the layer-2 row in `BUDGET.md`; status rows in
  `ARCHITECTURE.md`, `ROADMAP.md`, `CONFIG.md`, `README.md` and
  `config.example.yaml`.

Out:

- Reviewing, prompting, publishing — the engine adapter and publisher own
  those.
- Writing the resolved `head_sha` back to the queue row. The worker that
  claims the row is what knows the row; this returns the value.
- Sandboxing the *engine* (containers, seccomp, a read-only tool set). This
  issue guarantees nothing in the tree is executed **by the checkout**;
  `DESIGN.md` already specifies the read-only tool set for the review step.
- Layer 2's other two parts, path exclusions and the pre-flight token
  estimate. Both need a diff in hand to be worth anything, and the estimate
  needs the engine's tokeniser.

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
└── <owner>__<name>.git/        one bare mirror, incrementally fetched
    └── worktrees/…             per-run, detached, removed on teardown
```

The mirror carries the full commit graph, so `git merge-base` is always
answerable. The second review of the day fetches almost nothing. Two
concurrent runs are two worktrees over one object store, which is git's
designed use.

The one piece of shared mutable state is the mirror's ref namespace, and the
only operation that writes it is the fetch. An in-process `asyncio.Lock`
serialises fetches; that is sufficient, not merely convenient, because the
daemon is a single process — the same assumption the queue's per-PR lease
already rests on.

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
    co.diff        # git diff <merge_base>..<head>
```

1. **Size gate, before the first git invocation.** `additions + deletions`
   against `max_changed_lines`, `changed_files` against `max_changed_files`;
   `PullRequestTooLarge` otherwise. Raising here is what makes "refused before
   anything is written to disk" literally true rather than approximately true.
2. Ensure the mirror exists (`git init --bare` on first use), under the lock.
3. Fetch `refs/pull/{n}/head` into a run-scoped ref, and `refs/heads/{base_ref}`
   — `--no-tags --no-recurse-submodules`, under the lock.
4. Read the fetched sha. **A fork is not a special case**: `refs/pull/{n}/head`
   lives in the base repository for forks and branches alike, so one code path
   satisfies both halves of that acceptance item.
5. `git merge-base <base_tip> <head_sha>`.
6. `git worktree add --detach <run_dir> <head_sha>`, outside the lock.
7. Teardown in `finally`: `worktree remove --force`, `worktree prune`, delete
   the run-scoped ref.

**The fetched sha, not the API's, is what gets checked out and reported.** The
head can move between the two reads, and a review has to name the commit it
actually read. The publisher re-checks the live head before posting regardless
— that check belongs to publish time, as `QUEUE.md` sets out.

Unreferenced objects are reclaimed by git's own auto-gc once the run-scoped
ref is gone, so repeated runs settle rather than grow.

## 🛡 The hardened runner

Every `git` invocation goes through one function in `gitcmd.py`. Nothing else
in the package shells out, so the hardening cannot be forgotten at a call
site.

**An explicit environment, not the inherited one.** `GIT_CONFIG_GLOBAL` and
`GIT_CONFIG_SYSTEM` both at `/dev/null` is the important pair: the host's own
gitconfig is where `core.hooksPath`, credential helpers, aliases and the LFS
smudge filter are all defined, so neutralising it disables every one of them
at once rather than one flag per mechanism. With it, `GIT_TERMINAL_PROMPT=0`
and `GIT_ASKPASS=/bin/false`, so an anonymous fetch of something unreadable
fails in a second instead of blocking the daemon on a password prompt, and
`GIT_LFS_SKIP_SMUDGE=1`.

**Per-invocation `-c` flags**, as belt and braces:

| Flag | What it stops |
| :-- | :-- |
| `core.hooksPath=/dev/null` | A hook inherited from the mirror's config. |
| `protocol.allow=never`, `protocol.https.allow=always` | A submodule URL of the form `ext::sh -c …` — the classic route from "checked out a repo" to "ran attacker code". |
| `submodule.recurse=false` | Recursing into a submodule at all. |
| `filter.lfs.smudge=`, `filter.lfs.process=`, `filter.lfs.required=false` | An LFS filter executing during checkout. |

`stdin` is `DEVNULL`; each invocation has a wall-clock timeout and is killed
on expiry. `git submodule`, `git lfs`, and anything resembling a build,
install or test are never invoked.

**The tree is data.** The checkout executes nothing from it, and the engine
adapter inherits that contract rather than re-deciding it — the same rule
`DESIGN.md` already applies to diffs and comment bodies.

### The fetch is anonymous

No credential reaches git: not on the argv, not in the remote URL, not in
`.git/config`. The target repository is public, so the token the poller holds
buys nothing here, and not passing it is one fewer way to leak it to disk. A
private repository therefore fails the fetch loudly, which is the correct
outcome for a capability that has not been designed.

## 💰 The caps are layer 2, so they live in `budget`

```yaml
budget:
  max_changed_files: 100
  max_changed_lines: 5000
```

`BUDGET.md`'s layer 2 is "path exclusions, diff-size caps, pre-flight token
estimate", scheduled "with the engine adapter". The diff-size cap arrives
here instead, because refusing an oversized pull request is also how the disk
is protected — the issue's own observation. That row becomes *partly done*.

Putting the keys in `budget` rather than in `workspace` keeps every spending
cap in one specification and one section, which is what `CLAUDE.md` §5 asks
of code that bounds spend. It has a second consequence worth having:
**`budget` is the only section `SIGHUP` hot-swaps**, so the caps can be
tightened on a running daemon.

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
already is. It is **not** hot-swapped: moving the cache under a running
daemon would orphan the mirror, so a change is logged as needing a restart,
like `github`, `triggers` and `store`.

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

One added check: an anonymous `ls-remote` against the configured repository.
`DESIGN.md` lists `github.com` egress as a prerequisite to confirm, and the
poller's route answering says nothing about this one — they are different
hosts and, on an allowlist firewall, different rules.

## 🧪 What the tests must pin

Fixtures build a real repository in `tmp_path`, served over `file://`,
including a commit reachable **only** from `refs/pull/7/head` and from no
branch. That is precisely the shape a fork pull request has, so the fork
criterion is met offline rather than approximated. No test touches the
network.

Behaviour:

- a checkout is detached at the exact sha, for the branch case and the
  fork-shaped case;
- `head_sha` is resolved for a mention trigger, whose payload carries none;
- the diff is computed against the merge base, not the base tip;
- two concurrent checkouts of different pull requests both succeed and neither
  sees the other's files.

Bounds (`CLAUDE.md` §5):

- a pull request over either cap raises `PullRequestTooLarge` and leaves
  **nothing** new under `cache_dir` — asserted on the directory, not on a
  mock;
- the default caps are pinned by value;
- after teardown the worktree is gone and the run-scoped ref is deleted, and a
  second run over the same pull request returns the worktree and ref counts to
  their baseline.

Safety — each pins a real escape by making it available and asserting it does
not fire, never by asserting a flag appears in an argv:

- a hostile `GIT_CONFIG_GLOBAL` defining `core.hooksPath` at a hook that
  writes a marker file → no marker;
- a custom smudge filter defined the same way, with a matching
  `.gitattributes` → no marker. This exercises the LFS mechanism without
  git-lfs being installed;
- a gitlink entry whose submodule URL is `ext::`-shaped → the directory stays
  empty and nothing is executed.

`git` becomes a documented prerequisite in `DEVELOPER.md` and `DESIGN.md`, and
these tests **fail** rather than skip when it is absent: a safety test that
silently skips is a safety test that never runs.

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
| Two concurrent runs do not interfere | Per-run worktrees; lock on the fetch |
| Hooks, submodules, LFS disabled, pinned by tests | The hardened runner; three safety tests |
| Oversized pull request refused before disk | Step 1, asserted on `cache_dir` |
| Torn down; repeated runs do not grow disk | Step 7; baseline-count test |
| No test requires network | `file://` fixtures throughout |
