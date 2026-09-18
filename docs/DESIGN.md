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
| 4 | **Opaque limits.** Subscription plans expose no programmatic quota API. `claude -p --output-format json` reports what a run cost, but neither it nor any other agent CLI reports remaining allowance or a window reset time. | Querying the budget. Per-run token counts are reported; the plan's remaining budget is not, so enforcement must be self-maintained. |

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
| **Celery** | The same rejection as RabbitMQ, plus a framework on top: Celery dequeues in a broker while the ledger lives in SQLite, so no transaction spans both. See [Why not Celery](#-why-not-celery). |
| **Engine code inside DTaaS at `review-agents/`** | The reviewer's CI would run on every DTaaS change unless paths were filtered, its Python dependency tree would join the DTaaS supply chain, and reuse for a second repository would need vendoring or a monorepo-subdirectory dependency. |
| **API-key billing instead of the subscription** | Not rejected — see [Billing mode](#-billing-mode-unresolved) below. A live option that requires no redesign. |
| **Human review only (status quo)** | No new infrastructure or cost, but leaves the bottleneck unaddressed. |

## 🌿 Why not Celery

Celery has genuinely similar semantics to what [QUEUE.md](QUEUE.md) describes
— a durable task queue, bounded retries, a visibility timeout that behaves
like a lease, and `beat` for periodic work. It is the obvious thing to reach
for, so the reasons it does not fit are worth writing down.

**The blocking one: the reservation cannot be atomic with the dequeue.** The
governor [reserves inside the same transaction as the claim](BUDGET.md#-reserve-then-settle)
because three workers can each check the remaining allowance, each correctly
conclude there is budget, and collectively breach the cap. Under Celery the
dequeue happens in the broker and the ledger lives in SQLite, so there is no
transaction spanning both: what is left is a distributed transaction, or a
window between dequeue and reserve, which is the breach. Kombu does ship a
SQLAlchemy transport that could point at SQLite, but it has long been flagged
experimental rather than a supported production path — and it would not help
anyway, because the dequeue still happens inside kombu's own session rather
than one the governor can join.

The rest is mismatch rather than impossibility:

| Need | Celery | Cost |
| :-- | :-- | :-- |
| Adaptive 10 s → 600 s poll interval, snapping to the floor on activity | `beat` schedules are static; it needs a custom scheduler or self-rescheduling with `countdown=` | fights the framework for what is fifteen lines in `interval.py` |
| Enqueue at most once per [dedupe key](TRIGGERS.md#-dedupe-keys) | not native — delivery is at-least-once | implemented in the database regardless |
| One worker per pull request | not native | a broker-side lock, or `celery-singleton` |
| Owner-guarded `complete`/`release`, bounded attempts, an `abandoned` status | partly approximated by `acks_late` and the visibility timeout | the inspectable version is hand-rolled anyway |
| [No inbound port, no extra daemon](#-the-four-constraints) on a locked-down host | needs a broker process | a Redis or RabbitMQ service, a port and an ops surface |
| An `asyncio`-native client and poller | Celery 5 has no first-class async task execution | the poller runs outside Celery regardless |

The scale argument also runs backwards. Celery exists to fan work across many
workers; this topology is deliberately **one writer on one host**, and the
governor actively wants that serialisation. Horizontal scale here is a hazard
rather than an unused feature.

Two things it would genuinely buy: Flower gives operational visibility a
`while` loop does not, and task priorities would be a clean way to let an
explicit `@claude` overtake an auto-review, which today is `ORDER BY
enqueued_at`. Neither is worth a broker while the cheap local equivalents —
structured logging, and an ordering tweak — remain available.

Revisit under the condition [STORAGE.md](STORAGE.md#-why-sqlite) already
names: if the agent ever becomes multi-host. The move then is not
Celery-on-SQLite but PostgreSQL plus a broker, with the reservation becoming
an explicit row lock.

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
Codex CLI and Gemini CLI. The protocol and the record are implemented —
see [ENGINE.md](ENGINE.md); the adapters are not.

**Every adapter is a command-line tool, invoked as a subprocess. No vendor
SDK is linked.** That is a decision, not an accident of what shipped first:

- A CLI is the interface every one of these agents actually offers. Codex
  CLI, opencode and Gemini CLI have no Python SDK worth targeting, so an
  SDK-shaped seam would be a seam only Claude could fit through — the
  opposite of the point.
- A subprocess is a containment boundary a library call is not. The review
  step runs over an untrusted tree; a separate process with its own working
  directory, its own environment and a kill-on-timeout is a control the
  agent holds, whereas an in-process agent loop shares this process's
  memory, credentials and file handles.
- Dependency surface. An SDK pins a vendor's transitive tree into a daemon
  whose other dependencies are `httpx`, `PyYAML` and the standard library.
  A CLI is a version string and an `argv`.

The cost is real and worth naming: a subprocess boundary means parsing
whatever the CLI prints, so an output-format change breaks an adapter in a
way a typed SDK response would not. Each adapter therefore pins the CLI
version it was written against and fails loudly on an unparseable run
rather than treating it as an empty review.

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
CLI adapter only, and add one second engine plus a shared conformance suite in
the final phase. The seam pays for itself regardless — it is what makes the
review step testable without spending tokens.

## 💳 Billing mode

One clause narrowed this question considerably. The [Agent SDK
overview](https://code.claude.com/docs/en/agent-sdk/overview) states that
"unless previously approved, Anthropic does not allow third party developers
to offer claude.ai login or rate limits for their products, including agents
built on the Claude Agent SDK", and points such products at API keys.

**That clause does not bear on this agent, because the agent links no SDK.**
Every engine adapter is a command-line tool run as a subprocess — see
[Generalisation](#-generalisation-to-other-agents) — so the agent is a
*user* of Claude Code, not a product built on the Agent SDK, and it offers
nobody else a login or a rate limit. The credential never leaves the host and
no third party authenticates through it.

What remains is the narrower and older question, which the SDK note does not
answer: whether a persistent daemon running `claude -p` over **other
contributors'** pull requests counts as ordinary individual use of a personal
Max subscription. Anthropic's guidance is explicit that running the CLI from
cron or CI for one's own work is fine, and equally explicit that 24/7 bots and
business use belong on API keys. This sits between the two.

The design therefore keeps the escape hatch as configuration:
`billing.mode: subscription | api_key`. `api_key` mode uses the identical
governor with USD-denominated windows, removes the ambiguity entirely, gives
authoritative per-run cost, and eliminates the plan-lockout failure mode, at a
usage-based cost that is modest for this volume. Subscription mode is retained
for now, with the circuit breaker built as [BUDGET.md](BUDGET.md) specifies.

**Still to confirm before deploying in subscription mode:** the [Commercial
Terms](https://www.anthropic.com/legal/commercial-terms) and the consumer
usage policy, read against unattended Claude Code use specifically. The
citations and the conclusion belong in this section when that is done.

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
   `python -m pr_review_agent.bootstrap` on the host
   ([DEVELOPER.md](../DEVELOPER.md#-bootstrap-checks)); it now probes the
   git fetch route as well as the API, because `github.com` and
   `api.github.com` are different hosts and, on an allowlist-based firewall,
   different rules. This should be confirmed before implementation
   continues; it is the most common cause of schedule slip in this kind of
   deployment. The poller alone needs only `api.github.com`.
2. **The `git` binary, version 2.32 or later**, on the host's `PATH`. The
   [checkout](WORKSPACE.md) shells out to it, and 2.32 is where
   `GIT_CONFIG_GLOBAL` arrived — below that it is ignored *without an
   error*, so the control that neutralises the host's own gitconfig would be
   absent while appearing to be in force. Bootstrap checks the version
   before it checks the route, for that reason.
3. **The reviewer account** the agent posts as. Its numeric id goes in
   `github.agent_user_id`, which is required: without it the agent cannot
   recognise and skip its own comments, so the loader refuses to start
   rather than let it answer itself.
4. **The remaining allowlist members.**
5. **A GitHub token for the poller.** Read-only access to the three endpoints
   is enough for polling; write scope is only needed once the publisher
   exists. The checkout deliberately does **not** use it: the fetch is
   anonymous, so no credential can reach the git command line or `.git/config`.
