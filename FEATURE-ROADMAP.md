# Candidate features, drawn from five neighbouring projects

A survey of what five other pull-request and code-security projects do, and
which of it is worth having here. Nothing in this document is implemented.
It exists so that the ideas are recorded with their costs attached, rather
than rediscovered one at a time.

Every candidate is judged against the two constraints in
[CLAUDE.md](CLAUDE.md) §5 that a later fix cannot undo: **the agent spends a
shared metered budget**, and **it posts under a real account**. A feature
that widens either is not free, however good it looks.

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

Today `read_only_sandbox: true` is, as
[ENGINE.md](docs/ENGINE.md) admits, a claim about the argv the adapter
builds. The engine runs as the daemon's own user with the daemon's
filesystem and the daemon's network. A prompt injection that talks a model
into shelling out reaches `config.yaml`, `state.db`, `~/.ssh` and the
`GITHUB_TOKEN` in the environment.

**A1. Run the engine subprocess under `srt`.**
`sandbox-runtime` is an OS-level sandbox — bubblewrap on Linux, Seatbelt on
macOS — that wraps an arbitrary command. It is a CLI and an npm package,
so this is a dependency and a change to how `CliEngine` spawns, not copied
code. The confinement to aim for:

- `allowWrite`: the run's worktree and `/tmp` only.
- `denyRead`: `config.yaml`, `state.db`, `~/.ssh`, `~/.config/gh`, and the
  bare mirror's `config` (which holds the remote URL, and in this repository
  the remote URL holds a PAT).
- `allowedDomains`: the model API endpoint alone. Not `github.com` — the
  engine has no reason to reach GitHub; the poller and publisher do that.

This turns the fourth layer of the injection defence described in
[`engine/prompt.py`](src/pr_review_agent/engine/prompt.py) into a first
layer that does not depend on a model behaving.

**A2. Make `read_only_sandbox` mean the sandbox.**
Once A1 exists, the capability record can stop being a statement about
argv. An adapter that cannot be wrapped declares `false`, and the operator
sees which it is.

**A3. Treat a sandbox denial as a finding.**
`srt` reports violations. An engine run that tried to read `~/.ssh` is not a
failed run to be retried quietly — it is the strongest available signal that
the diff under review contained an injection. Log it, ledger it, and say so
in the posted comment.

**A4. Extend `host check` to the sandbox.**
`bubblewrap`, `socat` and `ripgrep` are the Linux prerequisites. An operator
should learn they are missing from `pr-review-agent host check`, not from the
first review that fails.

**Cost:** a new non-Python runtime dependency on the host, and a hard
failure mode if the sandbox misconfigures — a review that cannot read the
worktree produces nothing. Needs a config escape (`engine.sandbox: false`)
that is loud in the logs.

---

## 🛡 B. A deterministic gate before the model

`secure-code-agent`'s central idea is that an orchestrator should not be a
SAST engine: it invokes Bandit, Semgrep, Gitleaks, pip-audit, Trivy and
Checkov as subprocesses and unifies their output. Every one of those runs on
the worktree the [workspace](docs/WORKSPACE.md) already builds, and none of
them costs a token.

**B1. Run scanners on the worktree before the engine, and pass the findings
in as evidence.**
A leaked credential or a known-vulnerable dependency is then caught
deterministically, at zero cost, and the model's job shrinks to the part
that needs judgement. This is a budget feature as much as a security one.

**B2. A scanner-only rung at the bottom of the degradation ladder.**
[BUDGET.md](docs/BUDGET.md)'s ladder currently ends in refusal: past 100 %
of a window, nothing is reviewed. With B1 in place there is something
better to do than nothing — post the deterministic findings, say plainly
that the model pass was skipped for budget, and spend zero. A refused
review that still catches a committed secret is a large improvement on
silence.

**B3. Tiered findings (`FIX` / `REVIEW` / `ACCEPT`).**
`secure-code-agent` assigns a tier rather than emitting a flat list, so a
high-confidence secret is not presented alongside a noise-prone lint rule.
The report is already sectioned and numbered; a tier gives the publisher a
principled rule for what to cut when a report is too long for one comment,
instead of truncating by position.

**B4. Merge-base baselining.**
The tool's baseline file lets a legacy repository fail only on *new*
findings. A pull-request reviewer gets the baseline for free: run the
scanners at the merge base as well as at the head, and report the
difference. Without this, B1 on an old repository buries the diff's own
problems under a hundred pre-existing ones.

**B5. Detect a silenced finding on re-review.**
`--verify-against` re-audits and separates what was fixed from what was
suppressed by a lint disable. The reviewer is already round-aware, so it
already knows what it said last round. A contributor who answered a finding
with `# noqa` rather than a fix is exactly the thing a second-round review
should say out loud.

**B6. SARIF 2.1.0 output, and CWE/OWASP mapping.**
SARIF would let findings land in GitHub's code-scanning tab rather than a
comment. Flagged as a *deliberate open question*, not a recommendation: it
needs `security-events: write` on the token, which widens what the agent's
credential can do, and [PUBLISHER.md](docs/PUBLISHER.md)'s guarantee is
currently that it can make no write other than a comment. The
standards-mapping half (a CWE number on a scanner finding) costs nothing and
can land alone.

**Cost:** B1 adds several external binaries and their false-positive rates
to the operator's surface. It should be opt-in per scanner, and a scanner
that is absent or that crashes must not fail the review.

---

## 💬 C. Vocabulary, from pr-agent

`pr-agent` exposes distinct commands — `/describe`, `/review`, `/improve`,
`/ask` — rather than one monolithic review. Here, every trigger costs a full
review.

**C1. A verb after the mention.**
`@claude ask <question>` needs a fraction of a full review's tokens;
`@claude review` keeps today's behaviour as the default when no verb is
given. This is the cheapest available way to reduce average spend per
trigger, because most follow-up comments on a review are questions, not
requests to review again. Each verb needs its own pre-flight estimate and
its own bound, and [TRIGGERS.md](docs/TRIGGERS.md)'s reason codes need a
line per verb.

**C2. Incremental review.**
`pr-agent` reviews only what changed since the last reviewed commit. The
ledger already stores the `head_sha` of the previous run; diffing against it
rather than against the merge base would make round *n* cost roughly what
round *n* added. This is probably the largest single budget saving on the
list, and it interacts with the round-aware report that landed in #46.

**C3. Committable suggestions.**
GitHub renders a ` ```suggestion ` block as a one-click commit. It needs no
new permission and no new endpoint — it is a fenced block inside the comment
body the publisher already posts. The caveat is real: a suggestion is a
patch written by the model into a maintainer's one-click reach, so it
belongs behind a config flag and should be off by default.

**C4. Inline, line-anchored comments.**
The acceptance criterion in [ROADMAP.md](docs/ROADMAP.md) was reworded away
from "line-anchored" because inline comments require the reviews endpoint
and with it an `event` field. `pr-agent` does this and is MIT, so its
handling can be read and borrowed. The constraint to preserve is that
`event` is `COMMENT`, never `APPROVE` or `REQUEST_CHANGES`, pinned by a
test — the agent's inability to approve is a design guarantee, not a
default.

**C5. Per-repository review configuration.**
`pr-agent` drives review categories from a checked-in config. This project
reads `standards_paths` from the merge base already, and the same trust
argument applies, so the mechanism exists — what is missing is structured
knobs (which categories, which severities, which paths) rather than prose.

**Explicitly not recommended:** `pr-agent`'s multi-provider support
(GitLab, Bitbucket, Azure DevOps, Gitea) and its LiteLLM model abstraction.
[DESIGN.md](docs/DESIGN.md) chose one host and a CLI-subprocess engine seam
deliberately. Both would be large diffs bought against a requirement nobody
has stated.

---

## 🧪 D. Review dimensions, from kh-bikash/pr_agent

That project runs Security, Performance and Code Quality agents in parallel
and synthesises one markdown report from their JSON.

**D1. Optional per-dimension passes, each with its own token ceiling.**
The engine seam already supports this shape — it is the same adapter invoked
with a different prompt. What it must not become is three times the spend by
default. The governor has to reserve for the whole set before the first pass
starts, and a dimension that does not fit is dropped with a reason code
rather than run and then refused halfway.

**D2. Truncate-and-say-so as a ladder rung.**
`max_changed_files` and `max_changed_lines` currently refuse an oversized
pull request outright. Reviewing the largest hunks that fit and stating in
the comment what was left out is more useful than refusing, and the
[exclusions](src/pr_review_agent/workspace/exclusions.py) machinery already
knows how to rank what to drop.

---

## 📋 E. Transparency, from pr-automation-agent

AGPL, so this is an idea and not code. Its contribution is treating
AI authorship as a compliance obligation: an append-only JSONL audit trail,
and a header on every generated artefact.

**E1. An AI-disclosure line in every posted comment.**
Engine, model, run id, and the fact that a machine wrote it. The EU AI Act's
transparency obligation applies to this repository's likely deployment, and
the information is already in the ledger — it is a formatting change.

**E2. An append-only audit log that survives the retention sweep.**
The planned sweep purges review content from `runs` on merge. A JSONL line
per run — timestamp, actor id, trigger reason, engine, model, tokens,
outcome — is small, keeps the answer to "what did the agent do in March"
after the content is gone, and is trivially greppable by an auditor who does
not want to open SQLite.

---

## 🧭 Suggested order

Ordered by value per unit of diff, not by section:

1. **A1–A4, the sandbox.** The largest unclosed security gap, and it is a
   dependency rather than a design.
2. **C2, incremental review.** The largest budget saving, and the ledger
   already holds what it needs.
3. **B1 + B2, scanners and the scanner-only rung.** Turns budget exhaustion
   from silence into a cheap answer.
4. **E1, the disclosure line.** Nearly free.
5. **C1, verbs after the mention.** Reduces average cost per trigger; needs
   care in the trigger layer, which is the part of the system where a
   mistake is a stranger getting a review.
6. **B3–B5, tiering, baselining and silence detection.** Quality of the
   report rather than new capability.
7. **C3, C4, D1, D2.** Each is worth doing and none is urgent.
8. **B6, SARIF.** Only after a decision about widening the token's scope.

Nothing above should land without the bound it needs: a feature that widens
what triggers a review, or that raises what one can spend, says so in its
description and ships the test that pins the new limit.
