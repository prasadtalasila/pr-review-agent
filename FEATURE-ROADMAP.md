# Candidate features, drawn from five neighbouring projects

A survey of what five other pull-request and code-security projects do, and
which of it is worth having here. Nothing in this document is implemented.
It exists so that the ideas are recorded with their costs attached, rather
than rediscovered one at a time.

Every candidate is judged against the two constraints in
[CLAUDE.md](CLAUDE.md) §5 that a later fix cannot undo: **the agent spends a
shared metered budget**, and **it posts under a real account**. A feature
that widens either is not free, however good it looks. Each one below names
the module it would touch, because several ideas that read well in the
abstract turn out to be already solved here, or to be in tension with a
guarantee the code holds deliberately.

## 📚 The five sources, and what may be taken from each

| Project | Licence | What may be taken |
| :-- | :-- | :-- |
| [the-pr-agent/pr-agent](https://github.com/the-pr-agent/pr-agent) | MIT (© The PR Agent) | Code, with attribution. Usable as a dependency. |
| [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime) | Apache-2.0 | Usable as a direct dependency (`srt`, bubblewrap underneath). |
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
suggest. [`engine/cli.py`](src/pr_review_agent/engine/cli.py) builds the
child's environment from an **allowlist** — `PATH`, `HOME`, and whatever
matches `env_prefixes` — so `GITHUB_TOKEN` genuinely does not reach the
engine. [`engine/claude.py`](src/pr_review_agent/engine/claude.py) pins
`--tools Read,Grep,Glob` (no Bash, no Write, no Edit), `--restricted`,
`--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`
and `--permission-prompts none`. A `CLAUDE.md` in the tree under review is
not instructions to the reviewer, and there is no shell to escape into.

What remains is narrower and more specific than "the sandbox is missing":

- Nothing in that argv confines `Read`, `Grep` and `Glob` to the working
  directory. `cwd` is the worktree; an absolute path is not.
- `HOME` is passed through, and the process runs as the daemon's user, so
  `config.yaml`, `state.db` and the bare mirror's `config` are all readable.
  In this repository's own deployment the mirror's `config` holds the remote
  URL, and the remote URL holds a PAT.
- The output channel is public. `Finding.body` is a free-text string that
  [`publisher.py`](src/pr_review_agent/publisher.py) renders into a comment
  on a public pull request.

Those three compose into one chain: a diff that talks the reviewer into
reading a file outside the worktree and quoting it in a finding body has
exfiltrated it. No tool beyond the three that are already granted is needed.

**A1. Run the engine subprocess under `srt`.**
`sandbox-runtime` is an OS-level sandbox — bubblewrap on Linux, Seatbelt on
macOS — that wraps an arbitrary command, so this is a dependency and a change
to `CliEngine._start`, not copied code. The confinement to aim for:

- `allowRead`: the run's worktree. `denyRead`: `$HOME`, the config, the
  store, and the mirror.
- `allowWrite`: nothing but the worktree and `/tmp`.
- `allowedDomains`: the model API endpoint alone — not `github.com`, which
  the engine has no reason to reach.

This is what turns the containment from a property of the argv into a
property the kernel holds.

**A2. Give `Capabilities.read_only_sandbox` a consumer.**
[`engine/models.py`](src/pr_review_agent/engine/models.py) says plainly that
the field has none, and `claude.py`'s comment says it is "a claim about the
argv". Once A1 exists the field can mean the sandbox, an adapter that cannot
be wrapped declares `False`, and the worker can decline to run an
unconfinable engine over an untrusted tree.

**A3. Treat a sandbox denial as a finding, not a failure.**
`srt` reports violations. A run that tried to read `$HOME/.ssh` is the
strongest available evidence that the diff under review contained an
injection. It deserves a `StopReason` of its own in
[`budget.py`](src/pr_review_agent/budget.py) — alongside `TIMEOUT` and
`ENGINE_ERROR`, which exist for the same reason — and a line in the posted
comment.

**A4. Extend `host check` to the sandbox.**
`bubblewrap`, `socat` and `ripgrep` are the Linux prerequisites. The
bootstrap checks already exist; an operator should learn they are missing
there rather than from the first review that fails.

**Cost:** a non-Python runtime dependency on the host, and a new hard failure
mode — a sandbox that denies the worktree produces a review of nothing.
Needs a config escape that is loud in the logs, and `EngineUnavailable` is
the right shape for "the wrapper would not start", since it settles at a
provable zero.

---

## 🛡 B. A deterministic gate before the model

`secure-code-agent`'s central idea is that an orchestrator should not be a
SAST engine: it invokes Bandit, Semgrep, Gitleaks, pip-audit, Trivy and
Checkov as subprocesses and unifies their output into tiers. Every one of
those runs on the worktree
[`workspace/repo.py`](src/pr_review_agent/workspace/repo.py) already builds,
and none of them costs a token.

**B1. Run scanners on the worktree before the engine, and pass the findings
in as evidence.**
A leaked credential or a known-vulnerable dependency is then caught
deterministically, for free, and the model's job shrinks to the part that
needs judgement. This is a budget feature at least as much as a security
one. The subprocess discipline `CliEngine` already encodes — built
environment, wall clock, terminate-then-kill — is what these scanners should
be run under, and they inherit the A1 sandbox for the same reason the engine
does.

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
[`triggers/mention.py`](src/pr_review_agent/triggers/mention.py) answers a
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
[ROADMAP.md](docs/ROADMAP.md) was reworded away from "line-anchored" because
inline comments need the reviews endpoint and with it an `event` field.
`pr-agent` does this and is MIT, so its handling can be read and borrowed.
What must be preserved is the thing `publisher.py` is built around: today the
module has no code path that could approve anything, and a test asserts the
source contains no such token. Adding the reviews endpoint replaces that
absent capability with a guarded field. That is a genuine weakening, and it
should be taken knowingly, with `event: COMMENT` pinned by its own test.

**C5. Per-repository review configuration.**
`pr-agent` drives review categories from a checked-in config.
[`engine/standards.py`](src/pr_review_agent/engine/standards.py) already
reads instructions from the merge base — deliberately not from the head, so
a diff cannot rewrite the reviewer's instructions — and the same trust
argument covers structured settings read the same way. What is missing is
knobs (categories, severities, paths), not the mechanism.

**Explicitly not recommended:** `pr-agent`'s multi-provider support (GitLab,
Bitbucket, Azure DevOps, Gitea) and its LiteLLM model abstraction.
[DESIGN.md](docs/DESIGN.md) chose one host and a CLI-subprocess engine seam
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

## 🧭 Suggested order

Ordered by value per unit of diff, not by section:

1. **A1–A4, the sandbox.** The read-and-quote chain above is the largest
   unclosed gap, and it closes as a dependency rather than as a design.
2. **C2, incremental review.** The largest budget saving, and the ledger
   already holds the range it needs.
3. **B1 + B2, scanners and the scanner-only rung.** Turns budget exhaustion
   from silence into a cheap answer, and `Mode` already reaches the adapter
   unused.
4. **E1, the disclosure line.** Nearly free.
5. **C1, verbs after the mention.** Cuts average cost per trigger, but lands
   in the trigger layer, so it wants the most care per line on this list.
6. **B3–B5, provenance, baselining and silence detection.** Quality of the
   report rather than new capability.
7. **C3, C4, D1, D2.** Each is worth doing and none is urgent. C4 costs a
   guarantee; the other three do not.
8. **B6, SARIF.** Only after a decision about widening the token's scope.

Nothing above should land without the bound it needs: a feature that widens
what triggers a review, or that raises what one can spend, says so in its
description and ships the test that pins the new limit.
