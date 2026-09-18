# Workspace and checkout

How a pull request's code gets onto disk, why none of it is ever run, and
what is left behind afterwards. Implemented in
`src/pr_review_agent/workspace/`.

## 📦 What it does

Given the facts about one pull request, it produces a directory containing
that pull request's code at an exact commit, plus the diff against the merge
base, and removes the directory afterwards.

It does nothing else. No review, no prompt, no publishing, and no tokens. It
lands with **no caller**, exactly as the queue and the governor did: the
worker that will use it does not exist yet.

## 🚫 Why a checkout rather than the API diff

`GET /pulls/{n}/files` paginates and truncates on a large pull request;
`git diff` does not. A local diff costs no request, so it does not compete
with the poller for the rate-limit budget [POLLER.md](POLLER.md) depends on.
And a reviewer needs surrounding code, sibling files and `AGENTS.md` —
context that only exists in a checkout.

[DESIGN.md](DESIGN.md) already listed `github.com` among the egress
prerequisites. The design assumed a checkout from the beginning and never
wrote it down.

## 🪞 One mirror, a worktree per run

```text
cache_dir/
├── <owner>__<name>.git/        one bare mirror, incrementally fetched
└── runs/<run-id>/              per-run worktree, removed on teardown
```

The mirror carries the full commit graph, so `git merge-base` is always
answerable and the second review of the day fetches almost nothing.

**The run directories are siblings of the mirror, not children.**
`$GIT_DIR/worktrees/` is where git keeps each worktree's own `HEAD`, `index`
and `commondir`; a working tree placed there serves both roles at once and
reports git's administrative files as untracked.

The only shared mutable state is the mirror's ref namespace, and one
`asyncio.Lock` serialises every write to it — the fetch *and* the teardown's
ref deletion, because `update-ref -d` and a concurrent fetch contend for
`packed-refs.lock`. One process-wide lock suffices because the daemon is a
single process, the same assumption the [queue's per-PR
lease](QUEUE.md#-one-pull-request-one-worker) already rests on.

A fork is not a special case anywhere in this: `refs/pull/{n}/head` lives in
the base repository for forks and branches alike.

**Both refspecs are forced.** Without the `+` on the base branch, a
force-push upstream makes the fetch fail non-fast-forward — and then every
review of every pull request on that base fails until an operator
intervenes.

## 🛡 The tree is untrusted

A fork's pull request head is attacker-controlled content being written to
the host filesystem and then handed to an agent. [DESIGN.md](DESIGN.md)
already refuses the self-hosted-runner design for the neighbouring reason.

**The environment is the control, not the flags.** Every invocation goes
through one runner in `gitcmd.py`, which builds the child environment from
nothing rather than inheriting it. `GIT_CONFIG_GLOBAL` and
`GIT_CONFIG_SYSTEM` at `os.devnull` are the load-bearing pair: the host's
gitconfig is where `core.hooksPath`, `core.fsmonitor`, `diff.external`,
credential helpers and smudge filters are all defined, so nulling that one
file disables every one of them at once rather than needing a flag each.

`GIT_ALLOW_PROTOCOL=https` is a whitelist that overrides all `protocol.*`
config. It is **a constant with no parameter, config key or override.** The
obvious alternative, `-c protocol.allow=never`, is defeated by a single
`protocol.ext.allow=always` anywhere in a config the nulling missed — and an
`ext::` submodule URL is the shortest path from "checked out untrusted code"
to "ran it".

The per-invocation flags are redundancy, with two exceptions that stop
things config-nulling does not:

| Flag | What it stops |
| :-- | :-- |
| `core.symlinks=false` | A symlink resolving outside the worktree. |
| `transfer.fsckObjects=true` | A malicious pack — a tree containing `.GIT/`, a malformed `.gitmodules` — accepted at `index-pack`. |
| `core.hooksPath=<devnull>` | A hook planted in the mirror's own `hooks/`. |
| `submodule.recurse=false` | Recursion into a submodule. |
| `filter.lfs.*` | A filter **named `lfs`**, and only that one. |

### Symlinks are a leak, not an execution bug

This is the one worth stating plainly, because "nothing is executed" does
not cover it. A fork can replace `AGENTS.md` — a file this design says the
reviewer reads — with a symlink to `~/.claude/.credentials.json`. Nothing
runs; the reviewer simply reads the target and can quote it into a public
review comment.

`core.symlinks=false` makes git write a plain file containing the link
target instead. It has to be decided here, because no later layer can undo a
symlink that already exists.

### The diff is computed in the mirror

`git diff` honours the `.gitattributes` of the tree it runs in. A pull
request that adds `*.py -diff` renders its own Python changes as
`Binary files ... differ`, with `--numstat` reporting `-  -`: the reviewer
sees nothing, while the API's `additions` count looks entirely normal.

So the diff runs in the bare mirror, which does not read in-tree attributes,
with `--no-ext-diff`. The worktree still exists for the engine to read
surrounding files; it is simply not where the diff is produced.

### The fetch is anonymous

No credential reaches git: not on the argv, not in the remote URL, not in
`.git/config`. The target repository is public, so the poller's token buys
nothing here, and not passing it is one fewer way to leak it to disk. A
private repository fails the fetch loudly, which is the correct outcome for
a capability that has not been designed.

`tests/test_workspace.py::test_no_credential_reaches_the_wire` asserts this
against the requests the test double actually received, rather than against
the command line.

### What is not covered

Path traversal (`.git`, `.GIT`, `git~1`) is refused by git itself, with
nothing written. The deployment target is Linux with a case-sensitive
filesystem, which this design assumes.

Sandboxing the *engine* — containers, seccomp, a read-only tool set — is
[DESIGN.md](DESIGN.md)'s concern, not this module's. What this module
guarantees is that the checkout executes nothing and that the tree cannot
reach outside itself.

## 💰 The caps are layer 2 of the budget

```yaml
budget:
  max_changed_files: 100
  max_changed_lines: 5000
```

A pull request over either cap raises `PullRequestTooLarge` **before the
first git invocation**, which is what makes "refused before anything is
written to disk" literally rather than approximately true.

They live in `budget` rather than in `workspace` so that every spending cap
stays in one specification and one section — see
[BUDGET.md](BUDGET.md#-five-layers-cheapest-first), layer 2. That also makes
them reloadable: `budget` is the only section `SIGHUP` hot-swaps. The caps
are therefore **passed per checkout** rather than captured when the
`Workspace` is built, because a workspace holding a snapshot would silently
ignore a tightened cap, which is the exact failure the reload mechanism
exists to prevent.

Both exist because neither bounds the other: two thousand one-line files
pass a line cap and still bury the engine; one fifty-thousand-line generated
file passes a file cap.

**They bound what the engine reads, not the disk.** A fetch pulls every
object reachable from the head, so a commit that adds a 100 MB blob and a
later one that deletes it reports `additions: 0` and still downloads 100 MB.
Disk is bounded in practice by GitHub's own repository limits and by
watching `cache_dir`. `fetch --filter=blob:limit=N` is the git-native knob
if a real byte bound is ever wanted.

## 🧹 The startup sweep

A crash between `worktree add` and teardown leaves a worktree and a
run-scoped ref behind forever. `Workspace.sweep()` prunes stale worktrees,
deletes any leftover `refs/run/*`, and removes the `runs/` directory.

It also clears stale `*.lock` files, and **startup is the only safe moment
to do that**: mid-flight a lock may belong to a live process, while at
startup no git of ours is running. Those locks come from the one case a
timeout cannot avoid — `Process.kill()` is SIGKILL, and git removes its own
locks on SIGTERM but cannot on SIGKILL. The runner therefore terminates with
a grace period before killing, and the sweep catches whatever still
survives.

## 🔭 What the workspace does *not* do

- **It does not write `head_sha` back to the queue row.** It returns the
  resolved value; the worker that claimed the row is what knows the row.
- **It does not decide whether the head is still current.** The fetched sha
  is what gets checked out and reported — the head can move between the API
  read and the fetch, and a review has to name the commit it actually read.
  Re-checking against the live head immediately before posting belongs to
  the publisher, as [QUEUE.md](QUEUE.md#-what-the-queue-does-not-do) sets
  out.
- **It does not read the tree.** It puts it there.
