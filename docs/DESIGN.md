# Design rationale

Why this exists, the four constraints that ruled out every off-the-shelf
option, and what was considered and turned down. The mechanics live
elsewhere: [ARCHITECTURE.md](ARCHITECTURE.md) for the components,
[TRIGGERS.md](TRIGGERS.md) and [POLLER.md](POLLER.md) for the two that are
built, [BUDGET.md](BUDGET.md) for the spending rails.

## 🎯 The problem

Pull request review is a serial bottleneck on maintainer availability.
First-pass feedback on mechanical issues — missing test coverage, error
handling gaps, security-relevant changes, violations of project conventions —
is often delayed by days, which slows contributors and pushes review effort to
the end of a release cycle.

The goal is routine review feedback within a minute of a pull request being
opened or a maintainer asking for it, without any pull request leaving our
infrastructure and without ever exhausting the shared Claude usage allowance.

## 🚧 The four constraints

These are what make the problem interesting; each one removes a category of
solution.

| # | Constraint | What it rules out |
| :-- | :-- | :-- |
| 1 | **No inbound network access.** The host cannot receive GitHub webhooks. | Every webhook-driven integration. |
| 2 | **Restricted eligibility.** Only an explicitly configured set of contributors may be auto-reviewed; others are reviewed only when an allowlisted maintainer asks. | Anything whose trigger surface is "all pull requests". |
| 3 | **Hard usage ceiling with no overspend.** The backend is a Claude Max 5x subscription with a weekly threshold, and **all Claude surfaces share one usage pool**. | Anything unbounded. A runaway reviewer does not merely overspend — it locks maintainers out of their own interactive Claude Code sessions until the window resets. |
| 4 | **Opaque limits.** Subscription plans expose no programmatic quota API. Neither `claude -p --output-format json` nor the Agent SDK result object reports remaining allowance or a window reset time. | Querying the budget. Per-run token counts are reported; the plan's remaining budget is not, so enforcement must be self-maintained. |

Constraint 1 is why the design polls rather than listens. Constraint 2 is why
the allowlist is gated on the *commenter* for mentions, not the author.
Constraints 3 and 4 together are why there is a
[budget governor](BUDGET.md) at all, and why it keeps its own ledger.

## 🔑 The one rule

> **Nothing may call a review engine outside the budget governor.**

Two classes of mistake in this system are not recoverable by a later fix: the
agent spends a shared, metered allowance, and it posts under a real GitHub
account. A change that widens what triggers a review, or that removes a cap,
is therefore held to a higher bar than the rest of the code — it must say so
explicitly and add a test pinning the new bound. See `CLAUDE.md` §5.

The structural consequence is the phase ordering: the **budget governor lands
before the review worker**, so the spending rails exist before anything can
spend.

## 🧭 Alternatives considered

| Option | Why not |
| :-- | :-- |
| **`claude-code-action` on GitHub-hosted runners** | Least work by a wide margin, but checkout and execution happen on GitHub's infrastructure, and trigger eligibility and spend controls are limited to what the action exposes. Fails the private-execution requirement. |
| **Self-hosted GitHub Actions runner** | Genuinely viable under the no-inbound constraint — the runner long-polls outbound — and keeps GitHub's event filtering and audit trail for free. Rejected as the *primary* design because the budget governor, ledger and per-PR leasing need a persistent stateful service regardless. Retained as the documented fallback past roughly fifteen repositories, where polling economics stop favouring a daemon. **Must never be attached to a public repository**: fork pull requests can execute arbitrary workflow code on the host. |
| **Public relay VM forwarding webhooks to an outbound AMQP consumer** | Lowest latency, and the private host still needs no open port. Rejected for now: an internet-facing component and a second piece of infrastructure, for a latency gain of a few seconds that the 👀 acknowledgement already makes imperceptible. |
| **PostgreSQL instead of SQLite** | The topology is one writer on one host, so Postgres's multi-client concurrency is capability paid for and never used — while SQLite's write serialisation actively simplifies the hardest invariant in the system ([reserve-then-settle](BUDGET.md#-reserve-then-settle)). SQLite needs no daemon, no port and no DBA on a locked-down host, and `sqlite3` is in the standard library. Revisit only if the agent becomes multi-host. |
| **RabbitMQ as the queue** | Reasonable if already operational, but the budget reservation must be atomic with the dequeue — trivial in one database, awkward across a broker plus a separate store. |
| **Engine code inside DTaaS at `review-agents/`** | The reviewer's CI would run on every DTaaS change unless paths were filtered, its Python dependency tree would join the DTaaS supply chain, and reuse for a second repository would need vendoring or a monorepo-subdirectory dependency. |
| **API-key billing instead of the subscription** | Not rejected — see [Billing mode](#-billing-mode-unresolved) below. A live option that requires no redesign. |
| **Human review only (status quo)** | No new infrastructure or cost, but leaves the bottleneck unaddressed. |

## 🏛 Repository placement

The engine lives in its own repository, along a split that already exists in
the problem: **the engine is generic; the standards are per-repository.**

- The **engine** has no knowledge of DTaaS, has its own release cadence and
  dependency tree, and holds the GitHub App private key and the agent
  credential. That is a real security boundary, not tidiness. Adding a second
  repository to its scope later is a config change rather than a fork.
- The **standards** stay in the reviewed repository, versioned alongside the
  code they describe and changeable by a normal pull request: `AGENTS.md` for
  conventions, `.github/REVIEW.md` for review guidance,
  `review-standards/*.md` for per-dimension checklists.

The GitHub App should be registered under the **organisation**, even though
the engine source lives in a personal repository, so the reviewer's identity
and installation stay independent of where the code is hosted. An App is not
transferred cheaply; a repository is.

## 🔌 Generalisation to other agents

The design generalises behind a single seam. Polling, allowlisting, dedupe,
leasing, staleness detection, publishing and the window arithmetic are all
agent-agnostic; only the "run a review" step is Claude-specific. A
`ReviewEngine` protocol with a declared `Capabilities` record
(`structured_output`, `usage_reporting`, `read_only_sandbox`, `subagents`,
`prompt_caching`) admits adapters for opencode, GitHub Copilot CLI, aider,
Codex CLI and Gemini CLI.

Two limitations are worth stating honestly:

- Agents without schema-constrained output need a fenced-JSON prompt contract
  with a *budgeted* validation retry.
- **Agents that report no token usage force the governor onto proxy controls
  only** — run-count, wall-clock and turn caps. That is a materially weaker
  guarantee, which is why every ledger row records a `usage_confidence` of
  `exact` / `estimated` / `unavailable`.

Review standards should therefore be authored in the cross-agent `AGENTS.md`
plus `review-standards/*.md`, with the Claude adapter generating `CLAUDE.md`
from them. Recommended sequencing: define the interface early, ship the Claude
adapter only, and add one second engine plus a shared conformance suite in the
final phase. The seam pays for itself regardless — it is what makes the review
step testable without spending tokens.

## 💳 Billing mode (unresolved)

Anthropic's documented position is that OAuth/subscription authentication is
intended for *ordinary, individual* use of Claude Code and the Agent SDK, and
its own guidance points 24/7 bots and business use at API keys. Running the
CLI from cron or CI for one's own work is explicitly fine; a persistent daemon
reviewing **other contributors'** pull requests on one personal Max
subscription is a genuine grey area — not credential intermediation (the
credential never leaves our host), but not obviously individual use either.

The design treats this as configuration: `billing.mode: subscription | api_key`.
`api_key` mode uses the identical governor with USD-denominated windows,
removes the policy ambiguity, gives authoritative per-run cost, and eliminates
the plan-lockout failure mode entirely, at a usage-based cost that is modest
for this volume.

**The current terms should be checked against Anthropic's legal-and-compliance
documentation before deploying in subscription mode.**

## 🛡 Prompt injection is in scope

Pull request diffs and comment bodies are untrusted input, and a pull request
containing text instructing the reviewer to approve itself is a realistic
vector. Three mitigations, none of which relies on the model behaving:

1. The review prompt frames diffs and comments explicitly as data.
2. The worker runs with a read-only tool set.
3. The publisher takes no approval or merge action regardless of what a review
   concludes — output is always event `COMMENT`, never `REQUEST_CHANGES`, so a
   machine's judgement can neither block nor authorise a merge.

The same rule governs the code: untrusted input may never widen what the agent
is allowed to do. Allowlisting in particular is on the numeric user id and
never the login, because a login can be renamed and the freed name registered
by a stranger.

## 🗑 Retention

Review content is retained **until the pull request is merged**, then purged:
findings, rationales and code excerpts are deleted and `run.content_purged_at`
is stamped. Ledger rows are **never** deleted, because the rolling budget
windows are computed from historical usage. The split is therefore *purge
content, retain metrics*. A `max_age_days` backstop covers pull requests that
stay open indefinitely.

Comments already posted on GitHub live in the pull request and are outside
this policy. The operator runbook must say so, so it is not misread as a
deletion guarantee.

## ✅ Prerequisites still to confirm

1. **Outbound HTTPS from the deployment host** to `api.github.com`,
   `github.com`, `codeload.github.com` and `api.anthropic.com`. Run
   `python -m pr_review_agent.bootstrap` on the host to check the two that
   matter most ([DEVELOPER.md](../DEVELOPER.md#-bootstrap-checks)). On an
   allowlist-based firewall this should be confirmed before implementation
   continues; it is the most common cause of schedule slip in this kind of
   deployment. The poller alone needs only `api.github.com`.
2. **The reviewer account** the agent posts as. Its numeric id goes in
   `github.agent_user_id`; until it is set, the agent cannot recognise and
   skip its own comments.
3. **The remaining allowlist members.**
4. **A GitHub token for the poller.** Read-only access to the three endpoints
   is enough for polling; write scope is only needed once the publisher
   exists.
