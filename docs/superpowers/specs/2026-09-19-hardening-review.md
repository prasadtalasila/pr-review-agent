# Hardening review: the review subprocess as a containment boundary

Reference: `feat/detailed-review-reports` (`d997023`, identical tree to `main`
at `7b1ba5a` plus the 0.15.0 release commit).

Three external reference points, and what each one contributes:

- **Claude Code's own deployment guidance.** Its
  [secure deployment guide](https://code.claude.com/docs/en/agent-sdk/secure-deployment)
  and [sandbox environments](https://code.claude.com/docs/en/sandbox-environments)
  page are explicit that the tool set and permission mode are *in-process*
  controls, and that file tools, hooks and MCP servers run unconstrained on
  the host unless the whole process is wrapped. It ships
  [`@anthropic-ai/sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime)
  (bubblewrap on Linux, Seatbelt on macOS) for exactly that, plus a
  credential-injecting egress proxy pattern.
- **qlty.sh.** Its [security page](https://docs.qlty.sh/cloud/security)
  describes ephemeral Fargate containers destroyed after each run,
  short-lived scoped GitHub tokens, and *no stored clones*. The shape is:
  the analysis host holds no durable secret and no durable copy of the code.
- **The FreeBSD jail.** Not a deployment target here, but the right mental
  model: confinement as one declarative, reviewable artefact (`jail.conf`),
  capabilities default-off and enumerable (`allow.*`), and irreversible from
  inside (a jail cannot raise its own `securelevel`).

## What is already right, so it is not re-proposed

This is a well-defended codebase and most of the obvious advice is already
implemented. Recorded so nobody spends effort re-deriving it:

- The engine child's environment is an **allowlist**, not a denylist
  (`src/pr_review_agent/engine/cli.py:37`), pinned by a test that asserts
  `GITHUB_TOKEN` cannot reach it (`tests/test_cli_engine.py:225`).
- The git child's environment is built from nothing, with
  `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` nulled and `GIT_ALLOW_PROTOCOL`
  as a whitelist (`src/pr_review_agent/workspace/gitcmd.py:106`). The
  `ext::` submodule path is closed.
- The diff is computed **in the bare mirror**, so in-tree `.gitattributes`
  cannot hide content from the reviewer.
- The publisher holds "cannot approve or merge" as an *absent capability*
  rather than a guarded field, with a test reading the source.
- The allowlist is keyed on numeric user id, and rejects logins loudly.
- Prompt-injection defence is layered and the layers are honestly ranked:
  argv first, wording last (`engine/prompt.py` module docstring).

The gaps below are all in one place: **everything protecting the review
subprocess is a policy decision made inside a third-party binary, and
nothing is an OS boundary.**

---

## P0 — the lethal trifecta is complete today

The [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
is untrusted input + access to private data + an exfiltration channel. This
agent has all three:

1. **Untrusted input**: the diff, the tree, the PR and comment bodies.
2. **Private data**: the child inherits `HOME`
   (`engine/cli.py:37`). That reaches `~/.claude/.credentials.json`,
   `~/.config/gh/hosts.yml`, `~/.ssh`, the daemon's own `config.yaml` and
   `store.sqlite`, and any `EnvironmentFile` holding `GITHUB_TOKEN` — all
   readable by the same UID.
3. **Exfiltration channel**: `publisher.render` writes `Finding.title` and
   `Finding.body` **verbatim** into a public GitHub comment. No network tool
   is needed; the comment *is* the egress.

What stands between (1) and (2) is `--tools Read,Grep,Glob`, `--restricted`
and `--permission-prompts none` — all enforced *inside* the `claude`
process. That may well hold today. But it is an unpinned assumption about an
upgradeable vendor binary, and it contradicts this repo's own doctrine that
containment should be "argv a test can assert rather than prompt wording a
model can be talked out of" (`engine/claude.py` docstring). The argv is
asserted; that the argv still *means* what it meant is not.

### P0.1 Run the engine child under a dedicated unprivileged UID

The single highest-value change, and it needs no container. Give the
reviewer its own user whose `$HOME` contains the Claude credential and
nothing else. `config.yaml`, the SQLite store, the token `EnvironmentFile`
and the daemon's `~/.ssh` become unreadable at the kernel level rather than
by policy.

Mechanically: the daemon stays as it is, and `CliEngine._start` execs
through `setpriv --reuid … --regid … --clear-groups --no-new-privs` (or
`sudo -u`). The argv prefix is a tuple, so it is pinned by a test exactly
like the existing flags.

Pair with a systemd unit carrying `NoNewPrivileges=yes`, `PrivateTmp=yes`,
`ProtectHome=`/`ProtectSystem=strict`, `ReadOnlyPaths=`, and
`InaccessiblePaths=` over the config and store directories. There is no
deployment/hardening document in `docs/` at all today — `ROADMAP.md:126`
still lists it as unstarted — so this is also the natural home for it.

### P0.2 Wrap the child in an OS sandbox

On top of P0.1, not instead of it. Two viable routes:

- **`@anthropic-ai/sandbox-runtime`** — the vendor's own answer, bubblewrap
  underneath, JSON allowlists for paths and domains. Cost: it is a declared
  beta with an unstable config format, and it pulls an npm dependency into a
  daemon whose entire tree is `httpx`, `PyYAML` and the standard library.
- **`bwrap` invoked directly from `CliEngine._start`** — bind the worktree
  read-only, `--tmpfs` for everything writable, `--unshare-all` plus a
  slirp/proxy for the one endpoint needed. More work up front, but it is a
  fixed argv tuple in the module that already owns the subprocess boundary,
  with no new package dependency and no beta format to track.

Recommendation: **bwrap directly**, for the same reason the repo rejected
vendor SDKs — a subprocess and an argv are a smaller contract than a library.

### P0.3 Validate that the containment flags are still accepted

This is the sharpest concrete defect. `preflight` only *warns* on an
unexpected CLI version (`engine/claude.py`, `preflight`). A future `claude`
that renamed or dropped `--restricted` would either error out (fine) or
ignore it (not fine) — and the adapter would log a warning and review
anyway, unrestricted.

Fix: at preflight, probe `claude --help` and assert every flag in `argv()`
appears in it; refuse to run otherwise. Pure, offline, spends nothing,
fully testable — the trigger-suite standard from `CLAUDE.md` §5.

### P0.4 A secret canary on the outbound comment

The last line of defence for the trifecta, and cheap. Before publishing,
scan the rendered body for the live `GITHUB_TOKEN` value and for the first
bytes of the Claude credential file; refuse to post and log loudly on a hit.
It cannot catch an encoded secret, but it catches the straightforward case
and it converts a silent leak into an alert.

---

## P1 — egress, and the shape of the outbound comment

### P1.1 Default-deny egress for the engine child

The child must reach `api.anthropic.com` (plus `claude.ai` and
`platform.claude.com` for OAuth refresh on a subscription credential). It
must reach nothing else. With P0.1 in place this is one nftables rule keyed
on the reviewer's UID; with P0.2 it is the sandbox's own proxy. Note the
vendor's own caveat: a hostname allowlist without TLS termination is
defeatable by domain fronting, so treat it as depth, not as the boundary.

This is the qlty/Fargate-VPC lesson translated to a single host: the
analysis compute sits in a segment that can reach the model endpoint and
nothing else.

### P1.2 Neutralise the comment body

`publisher.render`'s docstring correctly argues that engine output is
harmless because the module can take no action. That covers *actions*. It
does not cover what the text does to **readers**:

- `@mentions` in a finding body notify arbitrary GitHub users from the
  agent's account.
- `#123` cross-links create references on unrelated issues.
- Raw HTML comments and crafted `<sub>` trailers can forge a second
  "Automated review…" footer, or a line that reads as an approval.
- An unbounded body can exceed GitHub's comment limit and fail the publish
  after the tokens are spent.

Suggested minimum: cap total rendered length with a truncation marker,
escape `@` and `#` at word start in engine-authored text, and strip HTML
comments. Cheap, pure functions, testable without tokens.

### P1.3 Resolve `git` and `claude` to absolute, pinned paths

`GIT = "git"` (`workspace/gitcmd.py`) and `binary = "claude"`
(`engine/claude.py`) both resolve through the inherited `PATH`, which
`cli_environment` passes through (`engine/cli.py:100`). A shadowed binary on
`PATH` is a total compromise of every control in this document. Make both
absolute and configurable, and check them at startup — the bootstrap already
has a place for exactly this kind of pre-flight.

---

## P2 — resource ceilings, ephemerality, and one declarative profile

### P2.1 The only ceilings today are wall-clock and tokens

`timeout_seconds=900` (`engine/claude.py:92`) and the budget governor bound
*time* and *spend*. Nothing bounds memory, process count, CPU or disk. Two
concrete exposures:

- A runaway or injected child can fork; nothing caps pids.
- `_gate` fires **after** the fetch, deliberately and correctly documented
  (`workspace/repo.py`, `_gate` docstring) — so an oversized pull request
  costs disk before it is refused, and nothing caps that disk. A
  pathological repository can fill `cache_dir`.

Add a cgroup v2 slice (`MemoryMax`, `TasksMax`, `CPUQuota`) covering both
the git and engine children, and a free-space check in `Workspace.sweep`
that refuses new checkouts below a floor.

### P2.2 Tighten and bound the on-disk residue

qlty's rule is "no stored clones". This design deliberately keeps a mirror
as a cache, which is a reasonable trade — but two things follow:

- `cache_dir` is `chmod 0700` (`workspace/repo.py:356`), yet `runs/` is
  created with the default umask (`workspace/repo.py:278`). On a multi-user
  host, an untrusted checked-out tree may be world-readable. Create it
  `0o700` and set the daemon's umask at startup.
- Nothing ever garbage-collects a mirror for a repository that has been
  removed from config. A retention sweep belongs beside the existing
  crash-recovery `sweep()`.

### P2.3 Put the confinement in one reviewable place

This is the jail lesson, and it is the one the repo has *already applied
once*: `gitcmd`'s docstring says the controls live in one module "so that
they cannot be forgotten at one of them." The engine side has not had the
same treatment — confinement is currently spread across `BASE_ENVIRONMENT`
(`engine/cli.py:37`), `env_prefixes` (`engine/claude.py:84`), `TOOLS`
(`engine/claude.py:40`) and the argv tuple (`engine/claude.py:106`), and
after P0 it would also span a `setpriv` prefix, a bwrap profile, a systemd
unit and an nftables rule.

A `jail.conf` is one file that answers "what can this thing touch?"
completely. The analogue here is a single `confinement.py` holding the whole
profile — uid, bind mounts, environment, tool set, egress — that every child
passes through, with the systemd and nftables fragments generated from or
checked against it. The property worth buying is not elegance; it is that
the answer to "did this change widen the reviewer's reach?" is a diff to one
file, which is exactly the bar `CLAUDE.md` §5 sets for spending and identity.

Two further jail properties worth naming as acceptance criteria:

- **Irreversible from inside**: `no_new_privs` plus dropped capabilities, so
  nothing the child does can re-widen the boundary.
- **Default-off and enumerable**: every capability the reviewer has should
  be a line in that file, never an inherited default.

### P2.4 Log hygiene

`GitCommandError` carries full argv and stderr; `EngineProtocolError`
carries 200 bytes of stdout; the worker logs these with `exc_info=True`.
All of it can be attacker-influenced text landing in the operator's journal.
Low severity — but the prompt is already logged as a digest for precisely
this reason, and the same argument applies to stderr.

---

## Tests that should land with any of the above

Per `CLAUDE.md` §5, a change to the containment must pin its new bound:

1. `cli_environment` contains no `GITHUB_*` name — **exists**
   (`tests/test_cli_engine.py:225`).
2. The `setpriv`/`bwrap` prefix is present in the child argv, element by
   element.
3. Every flag in `ClaudeCliEngine.argv` appears in the installed CLI's
   `--help` (P0.3), asserted against a captured fixture offline and against
   the real binary under the existing `live` marker.
4. **A canary test**: a file outside the worktree containing a known string,
   and a fixture diff carrying a direct prompt injection asking for it.
   Assert the string appears in no finding. This is the only test that
   actually measures the boundary rather than the configuration of it, and
   it belongs under the `live` marker beside `test_cli_engine_live.py`.
5. Render-layer: an `@mention` and an oversized body in a `Finding` do not
   survive into the published comment (P1.2).

## Open questions for the operator

- Is the deployment host single-tenant? If yes, P0.1 alone closes most of
  P0 and P0.2 becomes depth rather than necessity.
- Is `npm` acceptable on the host? That decides P0.2's two routes.
- Does the Claude credential in use refresh over the network? If it is an
  API key, the P1.1 allowlist drops to one domain.
