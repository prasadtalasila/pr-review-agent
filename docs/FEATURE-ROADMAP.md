# Candidate features, from five neighbouring projects and a hardening review

A survey of what five other pull-request and code-security projects do, and
which of it is worth having here. Nothing in this document is implemented.
It exists so that the ideas are recorded with their costs attached, rather
than rediscovered one at a time.

Sections A and F are now written from a second input: the hardening review of
19 September,
[superpowers/specs/2026-09-19-hardening-review.md](superpowers/specs/2026-09-19-hardening-review.md),
which read the same code against Claude Code's own
[secure-deployment guidance](https://code.claude.com/docs/en/agent-sdk/secure-deployment),
[qlty.sh's published security model](https://docs.qlty.sh/cloud/security), and
the FreeBSD jail as a model of confinement. It agrees with the survey about
where the gap is and disagrees about how to close it, so §A below has been
rewritten around its findings, and §F is new and comes entirely from it.

Every candidate is judged against the two constraints in
[CLAUDE.md](https://github.com/prasadtalasila/pr-review-agent/blob/main/CLAUDE.md) §5 that a later fix cannot undo: **the agent spends a
shared metered budget**, and **it posts under a real account**. A feature
that widens either is not free, however good it looks. Each one below names
the module it would touch, because several ideas that read well in the
abstract turn out to be already solved here, or to be in tension with a
guarantee the code holds deliberately.

## 📚 The five sources, and what may be taken from each

| Project | Licence | What may be taken |
| :-- | :-- | :-- |
| [the-pr-agent/pr-agent](https://github.com/the-pr-agent/pr-agent) | MIT (© The PR Agent) | Code, with attribution. Usable as a dependency. |
| [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime) | Apache-2.0 | Usable as a direct dependency (`srt`, bubblewrap underneath), though §A2 argues for calling bubblewrap directly instead. |
| [marshallguillory86/secure-code-agent](https://github.com/marshallguillory86/secure-code-agent) | MIT (© Marshall Guillory) | Code, with attribution. |
| [kh-bikash/pr_agent](https://github.com/kh-bikash/pr_agent) | none stated | Inspiration only. No licence grant means no copying. |
| [VinitaSilaparasetty/pr-automation-agent](https://github.com/VinitaSilaparasetty/pr-automation-agent) | AGPL-3.0 | Inspiration only. Copying would relicense this project. |

Two of the five are only loosely related to reviewing pull requests.
`pr-automation-agent` is a data-ingest scaffolding framework whose connection
to this work is its compliance posture, and `kh-bikash/pr_agent` is a
demonstration web app. Both still contribute one idea each, recorded below.

---

## 🔒 A. Confining the engine for real

Start from what the code already does, because it is more than the docs
suggest. [`engine/cli.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/engine/cli.py) builds the
child's environment from an **allowlist** — `PATH`, `HOME`, and whatever
matches `env_prefixes` — so `GITHUB_TOKEN` genuinely does not reach the
engine. [`engine/claude.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/engine/claude.py) pins
`--tools Read,Grep,Glob` (no Bash, no Write, no Edit), `--restricted`,
`--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`
and `--permission-prompts none`. A `CLAUDE.md` in the tree under review is
not instructions to the reviewer, and there is no shell to escape into.

What remains is narrower and more specific than "the sandbox is missing".
The hardening review states it as the
[lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
— untrusted input, private data, and a channel out — and this agent has all
three at once:

- **Untrusted input**: the diff, the tree, the pull request and comment
  bodies. Nothing in the argv confines `Read`, `Grep` and `Glob` to the
  working directory either. `cwd` is the worktree; an absolute path is not.
- **Private data**: `HOME` is passed through and the child runs as the
  daemon's user, so `~/.claude/.credentials.json`, `~/.ssh`, `config.yaml`,
  the SQLite store and the bare mirror's `config` are all readable. In this
  repository's own deployment the mirror's `config` holds the remote URL, and
  the remote URL holds a PAT.
- **Exfiltration channel**: `Finding.title` and `Finding.body` are free text
  that [`publisher.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/publisher.py) renders **verbatim**
  into a comment on a public pull request. No network tool is needed; the
  comment is the egress.

Those three compose into one chain: a diff that talks the reviewer into
reading a file outside the worktree and quoting it in a finding body has
exfiltrated it, using no tool beyond the three already granted. What stands
in the way today is `--tools`, `--restricted` and `--permission-prompts
none`, every one of them enforced *inside* an upgradeable vendor binary.
That the argv is asserted by a test is not the same as the argv still
meaning what it meant, which is the gap the items below close.

**A1. Run the engine child under a dedicated unprivileged uid.**
The highest value per line on this list, and it needs no container. Give the
reviewer its own user whose `$HOME` holds the model credential and nothing
else; `config.yaml`, the store, the token `EnvironmentFile` and the daemon's
`~/.ssh` then become unreadable at the kernel level rather than by policy.
Mechanically the daemon is unchanged and `CliEngine._start` execs through
`setpriv --reuid … --regid … --clear-groups --no-new-privs`. The prefix is a
tuple, so a test pins it element by element exactly as the existing flags are
pinned. It pairs with a systemd unit carrying `NoNewPrivileges=yes`,
`PrivateTmp=yes`, `ProtectHome=`, `ProtectSystem=strict` and
`InaccessiblePaths=` over the config and store — and there is no deployment
or hardening document in `docs/` at all today, so this is where one starts.

**A2. Wrap the child in an OS sandbox — `bwrap` directly.**
On top of A1, not instead of it. `anthropics/sandbox-runtime` is the
vendor's own answer and wraps bubblewrap with JSON allowlists for paths and
domains, but it is a declared beta with an unstable config format and it
pulls an npm dependency into a daemon whose whole tree is `httpx`, `PyYAML`
and the standard library. Invoking `bwrap` from `CliEngine._start` — the
worktree bound read-only, `--tmpfs` over everything writable,
`--unshare-all` — is more work up front and stays a fixed argv tuple in the
module that already owns the subprocess boundary. The review recommends
`bwrap` for the same reason this project rejected vendor SDKs: a subprocess
and an argv are a smaller contract than a library. Either way the shape of
the confinement is the same — read the worktree, write the worktree and
`/tmp`, reach the model endpoint and nothing else, and in particular not
`github.com`, which the engine has no reason to touch.

**A3. Assert at preflight that the containment flags still exist.**
The sharpest concrete defect on the list. `preflight` in
`engine/claude.py` only *warns* on an unexpected CLI version, so a future
`claude` that renamed or dropped `--restricted` would either error out (fine)
or ignore it (not fine) — and the adapter would log a warning and review
anyway, unrestricted. Probe `claude --help` and refuse to run unless every
flag in `argv()` appears in it. Pure, offline, spends nothing, and testable
against a captured fixture: the trigger-suite standard from
[CLAUDE.md](https://github.com/prasadtalasila/pr-review-agent/blob/main/CLAUDE.md) §5.

**A4. Default-deny egress for the engine child.**
The child needs `api.anthropic.com`, plus `claude.ai` and
`platform.claude.com` if the credential refreshes over the network. It needs
nothing else. With A1 in place that is one nftables rule keyed on the
reviewer's uid; with A2 it is the sandbox's own proxy. The vendor's caveat
applies — a hostname allowlist without TLS termination is defeatable by
domain fronting — so this is depth, not the boundary.

**A5. Resolve `git` and `claude` to absolute, pinned paths.**
`GIT = "git"` in [`workspace/gitcmd.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/workspace/gitcmd.py)
and `binary = "claude"` in `engine/claude.py` both resolve through the
inherited `PATH`, which `cli_environment` passes through. A shadowed binary
on `PATH` defeats every other control in this section. Make both absolute
and configurable, and check them where the bootstrap already does its
pre-flight.

**A6. Neutralise the outbound comment, and canary it.**
`publisher.render`'s docstring argues correctly that engine output is
harmless because the module can take no action. That covers actions, not
what the text does to readers: an `@mention` in a finding body notifies
arbitrary users from the agent's account, `#123` cross-links an unrelated
issue, HTML comments and a crafted `<sub>` trailer can forge a second
"Automated review…" footer or a line that reads as an approval, and an
unbounded body can exceed GitHub's comment limit and fail the publish after
the tokens are spent. Cap the rendered length with a truncation marker,
escape `@` and `#` at word start in engine-authored text, and strip HTML
comments — all pure functions, testable without tokens. Alongside it, scan
the rendered body for the live `GITHUB_TOKEN` value and the first bytes of
the model credential file and refuse to post on a hit. That canary cannot
catch an encoded secret, but it catches the straightforward one and turns a
silent leak into an alert.

**A7. Give `Capabilities.read_only_sandbox` a consumer.**
[`engine/models.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/engine/models.py) says plainly that
the field has none, and `claude.py`'s comment says it is "a claim about the
argv". Once A1 and A2 exist the field can mean the sandbox, an adapter that
cannot be wrapped declares `False`, and the worker can decline to run an
unconfinable engine over an untrusted tree.

**A8. Treat a sandbox denial as a finding, not a failure.**
A run that tried to read `$HOME/.ssh` is the strongest available evidence
that the diff under review contained an injection. It deserves a
`StopReason` of its own in [`budget.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/budget.py) —
alongside `TIMEOUT` and `ENGINE_ERROR`, which exist for the same reason —
and a line in the posted comment.

**A9. Extend `host check` to the sandbox.**
`setpriv`, `bubblewrap` and `ripgrep` are the Linux prerequisites. The
bootstrap checks already exist; an operator should learn they are missing
there rather than from the first review that fails.

**A10. Put the whole confinement in one file.**
This is the jail lesson, and the repository has already applied it once:
`gitcmd`'s docstring says its controls live in one module "so that they
cannot be forgotten at one of them." The engine side never got the same
treatment. Confinement is spread across `BASE_ENVIRONMENT`, `env_prefixes`,
`TOOLS` and the argv tuple, and after A1–A4 it would also span a `setpriv`
prefix, a bwrap profile, a systemd unit and an nftables rule. A single
`confinement.py` holding the profile — uid, bind mounts, environment, tool
set, egress — with the systemd and nftables fragments generated from it or
checked against it, makes "did this change widen the reviewer's reach?" a
diff to one file. That is exactly the bar [CLAUDE.md](https://github.com/prasadtalasila/pr-review-agent/blob/main/CLAUDE.md) §5 sets for
spending and identity. Two jail properties are worth naming as acceptance
criteria: the boundary is irreversible from inside (`no_new_privs`, dropped
capabilities), and every capability the reviewer holds is a line in that
file rather than an inherited default.

**The test that measures the boundary.** Most of the tests above check
configuration. One checks containment: a file outside the worktree holding a
known string, a fixture diff carrying a direct prompt injection asking for
it, and an assertion that the string appears in no finding. It belongs under
the existing `live` marker beside `test_cli_engine_live.py`.

**Cost:** a non-Python runtime dependency on the host, an operator step that
did not exist before (creating the reviewer user), and a new hard failure
mode — a sandbox that denies the worktree produces a review of nothing.
Needs a config escape that is loud in the logs, and `EngineUnavailable` is
the right shape for "the wrapper would not start", since it settles at a
provable zero.

**What the operator decides first.** Three answers change the shape of this
section: whether the deployment host is single-tenant (if so, A1 alone closes
most of the gap and A2 becomes depth rather than necessity), whether `npm` is
acceptable on the host (that is the whole of the `srt`-versus-`bwrap`
choice), and whether the model credential in use refreshes over the network
(if it is a plain API key, A4's allowlist drops to one domain).

---

## 🛡 B. A deterministic gate before the model

`secure-code-agent`'s central idea is that an orchestrator should not be a
SAST engine: it invokes Bandit, Semgrep, Gitleaks, pip-audit, Trivy and
Checkov as subprocesses and unifies their output into tiers. Every one of
those runs on the worktree
[`workspace/repo.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/workspace/repo.py) already builds,
and none of them costs a token.

**B1. Run scanners on the worktree before the engine, and pass the findings
in as evidence.**
A leaked credential or a known-vulnerable dependency is then caught
deterministically, for free, and the model's job shrinks to the part that
needs judgement. This is a budget feature at least as much as a security
one. The subprocess discipline `CliEngine` already encodes — built
environment, wall clock, terminate-then-kill — is what these scanners should
be run under, and they inherit the A1 uid and the A2 sandbox for the same
reason the engine does.

**B2. A scanner-only rung at the bottom of the degradation ladder.**
`Mode` in `budget.py` is `FULL`, `MENTION_ONLY`, `EXHAUSTED`, and `EXHAUSTED`
means nothing runs. With B1 in place there is something better to do than
nothing: post the deterministic findings, say the model pass was skipped for
budget, and spend zero. `ReviewRequest.mode` already reaches the engine, and
`ClaudeCliEngine.argv` currently discards it with `del request` and a comment
saying the mode-aware argv is still to come — so the seam for this exists and
is unused.

**B3. Record where a finding came from.**
`Severity` and the publisher's `SECTIONS` already tier the report by
seriousness, so the tiering idea is half-built. What the type cannot express
is *confidence*: a Gitleaks hit and a model's opinion would render under the
same heading. `budget.UsageConfidence` is the precedent — the codebase
already refuses to collapse "unknown" into "zero" — and the same refusal
applies here. A provenance field also gives the publisher a principled rule
for what to cut when a report will not fit one comment, instead of cutting by
position.

**B4. Baseline at the merge base.**
`secure-code-agent` keeps a baseline file so a legacy repository fails only
on *new* findings. A pull-request reviewer gets the baseline for free:
`Checkout` already resolves and holds `merge_base`, so scanning both ends and
reporting the difference needs no new state. Without it, B1 on an old
repository buries the diff's own problems under a hundred pre-existing ones.

**B5. Detect a silenced finding on re-review.**
`--verify-against` re-audits and separates what was fixed from what was
suppressed by a lint disable. `ReviewRequest.prior` already carries the last
round's findings and `numbering.assign` already keeps identity stable across
rounds — the docstring notes that a gap in the numbering is what says an
item was fixed. A contributor who answered finding 4 with `# noqa` rather
than a fix is exactly what round two should say out loud, and the machinery
to notice it is almost all there.

**B6. SARIF 2.1.0 output, and CWE/OWASP mapping.**
Flagged as an *open question*, not a recommendation. SARIF would put findings
in GitHub's code-scanning tab, but it needs `security-events: write` on the
token, and `publisher.py`'s guarantee is that the only writes it knows how to
make are a reaction and an issue comment — held by an absent capability and
asserted by a test that reads the source. Widening the token is a decision
about that guarantee, not a formatting change. The standards-mapping half (a
CWE number carried on a scanner finding) costs nothing and can land alone.

**Cost:** B1 adds several external binaries, and their false-positive rates,
to the operator's surface. Each scanner should be opt-in, and one that is
absent or that crashes must not fail the review.

---

## 💬 C. Vocabulary, from pr-agent

`pr-agent` exposes distinct commands — `/describe`, `/review`, `/improve`,
`/ask` — rather than one monolithic review. Here,
[`triggers/mention.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/triggers/mention.py) answers a
single boolean question, `has_mention`, and every trigger costs a full
review.

**C1. A verb after the mention.**
`@claude ask <question>` needs a fraction of a review's tokens; a bare
`@claude` keeps today's behaviour. This is the cheapest available way to cut
average spend per trigger, because most follow-up comments on a review are
questions rather than requests to review again. The prose-stripping in
`strip_non_prose` is what makes parsing a verb safe — it already blanks code
fences, indented code, inline spans and blockquotes — but the return type has
to become a verb rather than a bool, the classifier needs a reason code per
verb, and each verb needs its own pre-flight estimate and its own bound.
This touches the trigger layer, which is the part of the system where a
mistake means a stranger getting a review, so it wants more care than its
size suggests.

**C2. Incremental review.**
`pr-agent` reviews only what changed since the last reviewed commit.
`Checkout` computes `merge_base..head` every round, so round five re-reads
everything rounds one to four already read. The ledger records the previous
run's `head_sha` — the publisher re-reads the live head precisely to compare
against it — so the range for an incremental diff is already stored. This is
probably the largest single budget saving on the list. Two things have to
hold: the exclusion `pathspec` applies to the narrower range unchanged, and
`ReviewRequest.prior` is what keeps an incremental round able to say "still"
truthfully about a finding whose lines it no longer sees.

**C3. Committable suggestions.**
GitHub renders a ` ```suggestion ` block as a one-click commit. It needs no
new endpoint and no new permission — it is a fenced block inside the comment
body the publisher already posts. The caveat is real: it puts a
model-written patch within one click of a maintainer, and the patch text
comes from a run over an attacker-influenced tree. Config flag, off by
default.

**C4. Inline, line-anchored comments.**
`Finding` already carries `path` and `line`; the acceptance criterion in
[ROADMAP.md](ROADMAP.md) was reworded away from "line-anchored" because
inline comments need the reviews endpoint and with it an `event` field.
`pr-agent` does this and is MIT, so its handling can be read and borrowed.
What must be preserved is the thing `publisher.py` is built around: today the
module has no code path that could approve anything, and a test asserts the
source contains no such token. Adding the reviews endpoint replaces that
absent capability with a guarded field. That is a genuine weakening, and it
should be taken knowingly, with `event: COMMENT` pinned by its own test.

**C5. Per-repository review configuration.**
`pr-agent` drives review categories from a checked-in config.
[`engine/standards.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/engine/standards.py) already
reads instructions from the merge base — deliberately not from the head, so
a diff cannot rewrite the reviewer's instructions — and the same trust
argument covers structured settings read the same way. What is missing is
knobs (categories, severities, paths), not the mechanism.

**Explicitly not recommended:** `pr-agent`'s multi-provider support (GitLab,
Bitbucket, Azure DevOps, Gitea) and its LiteLLM model abstraction.
[DESIGN.md](DESIGN.md) chose one host and a CLI-subprocess engine seam
deliberately, and `CliEngine` is built on the assumption that no vendor SDK
is linked. Both would be large diffs bought against a requirement nobody has
stated.

---

## 🧪 D. Review dimensions, from kh-bikash/pr_agent

That project runs Security, Performance and Code Quality agents in parallel
and synthesises one markdown report from their JSON.

**D1. Optional per-dimension passes, each with its own ceiling.**
The engine seam already supports this shape — the same adapter, a different
prompt. What it must not become is three times the spend by default. The
governor reserves before a run and settles after, so the whole set has to be
reserved before the first pass starts; a dimension that does not fit is
dropped with a reason code rather than run and then refused halfway.

**D2. Truncate and say so, instead of refusing.**
`Checkout._gate` raises on `max_changed_files` and `max_changed_lines`, so an
oversized pull request gets no review at all. Reviewing what fits and stating
in the comment what was left out is more useful. `exclusions.pathspec` is
where this belongs, because it is already the one mechanism that feeds both
the `--numstat` the gate counts and the `git diff` the engine is shown — a
truncation applied there cannot show the engine something the gate did not
count.

**Explicitly not needed:** that project's "robust JSON extraction" from
markdown-fenced model output. `ClaudeCliEngine` passes `--json-schema` and
parses a result envelope strictly, raising `EngineProtocolError` rather than
degrading to an empty review. That is the stronger design and it is already
here.

---

## 📋 E. Transparency, from pr-automation-agent

AGPL, so this is an idea and not code. Its contribution is treating AI
authorship as a compliance obligation: an append-only JSONL audit trail, and
a generated-by header on every artefact.

**E1. An AI-disclosure line in every posted comment.**
Engine, model, run id, and the fact that a machine wrote it. `publisher.py`
already emits a fixed footer said once on every comment, and `runs` already
joins a comment to the ledger rows that paid for it, so this is a formatting
change over data that exists. The EU AI Act's transparency obligation applies
to this project's likely deployment.

**E2. An append-only audit log that survives the retention sweep.**
The planned sweep purges review content from `runs` on merge, and
`RunStore.purge_content` is already there waiting for a caller. A JSONL line
per run — timestamp, actor id, trigger reason, engine, model, tokens,
`stop_reason`, outcome — is small, answers "what did the agent do in March"
after the content is gone, and is greppable by an auditor who will not open
SQLite. Every field it needs is already recorded.

---

## ⚖️ F. Ceilings, residue and log hygiene

From the hardening review, and from qlty.sh's rule that the analysis host
holds no durable secret and no durable copy of the code. This project keeps
a bare mirror as a cache, which is a defensible trade — but it is a trade,
and these are the things that follow from it.

**F1. Bound memory, processes and CPU, not only wall clock.**
`timeout_seconds=900` and the budget governor bound *time* and *spend*;
nothing bounds anything else. A runaway or injected child can fork and
nothing caps pids. A cgroup v2 slice (`MemoryMax`, `TasksMax`, `CPUQuota`)
covering both the git and engine children is the one mechanism that covers
all of it, and it is configuration rather than code.

**F2. Bound the disk the fetch spends before the gate fires.**
`Checkout._gate` fires **after** the fetch, deliberately and correctly — the
docstring in [`workspace/repo.py`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/workspace/repo.py)
says why. The consequence is that an oversized pull request costs disk
before it is refused, and nothing caps that disk, so a pathological
repository can fill `cache_dir`. A free-space floor checked in
`Workspace.sweep`, refusing new checkouts below it, is the smaller half of
the fix; §D2's truncation is the other half.

**F3. Tighten the on-disk residue.**
`cache_dir` is created `0700`, but `runs/` is created with the default
umask, so on a multi-user host an untrusted checked-out tree may be
world-readable. Create it `0o700` and set the daemon's umask at startup.
Separately, nothing ever collects the mirror of a repository that has been
removed from config; that retention sweep belongs beside the existing
crash-recovery `sweep()`.

**F4. Log hygiene.**
`GitCommandError` carries full argv and stderr, `EngineProtocolError` carries
200 bytes of stdout, and the worker logs both with `exc_info=True` — all of
it attacker-influenced text landing in the operator's journal. Low severity,
but the prompt is already logged as a digest for exactly this reason, and
the argument does not stop at the prompt.

**Cost:** F1 and F3 are close to free. F2 introduces a refusal an operator
has to be able to read in the logs, or a full disk becomes a silent stall.

---

## 🧭 Suggested order

Ordered by value per unit of diff, not by section:

1. **A3, the flag check, and A5, absolute binaries.** Both are small, pure,
   offline and spend nothing, and each one is a live defect rather than a
   missing layer: today an upgraded CLI can silently stop being restricted,
   and a shadowed `PATH` entry defeats everything else here.
2. **A1, the reviewer's own uid.** The largest reduction in what a
   read-and-quote chain can reach, bought with a `setpriv` prefix and a
   systemd unit rather than a design.
3. **A6, the outbound comment.** Escaping, a length cap and the secret
   canary — pure render-layer functions, and the canary is the only thing
   standing between a leak and a public comment.
4. **C2, incremental review.** The largest budget saving, and the ledger
   already holds the range it needs.
5. **A2 and A4, the sandbox and default-deny egress.** Depth on top of A1,
   and the point where the operator's answers about tenancy and `npm`
   decide the route.
6. **B1 + B2, scanners and the scanner-only rung.** Turns budget exhaustion
   from silence into a cheap answer, and `Mode` already reaches the adapter
   unused.
7. **E1, the disclosure line, and F1/F3, the cheap ceilings.** Nearly free.
8. **C1, verbs after the mention.** Cuts average cost per trigger, but lands
   in the trigger layer, so it wants the most care per line on this list.
9. **A7–A10, F2, F4.** The consumers, the reason code, the host check, the
   single confinement file and the remaining residue. A10 is worth more the
   later it is left undone, because it is what stops A1–A4 from scattering.
10. **B3–B5, provenance, baselining and silence detection.** Quality of the
    report rather than new capability.
11. **C3, C4, D1, D2.** Each is worth doing and none is urgent. C4 costs a
    guarantee; the other three do not.
12. **B6, SARIF.** Only after a decision about widening the token's scope.

Nothing above should land without the bound it needs: a feature that widens
what triggers a review, or that raises what one can spend, says so in its
description and ships the test that pins the new limit.
