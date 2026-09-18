# Roadmap and acceptance criteria

What is built, what is next, and the checklist the finished agent has to
satisfy. The criteria come from the feature issue that opened the project;
they are reproduced here so they survive the issue being closed.

## 📍 Where things stand

| Component | State |
| :-- | :-- |
| Classifier / allowlist / mention parsing | implemented, unit tested |
| Config loader (`config.yaml`) | implemented, unit tested |
| Poller (async, ETag conditional requests) | implemented, unit tested |
| Payload mapping (REST dicts → trigger models) | implemented, unit tested |
| SQLite store (watermarks, ETags, migrations) | implemented, unit tested |
| Queue and per-PR lease | implemented, unit tested |
| Bootstrap checks for a new host | implemented, unit tested |
| Daemon loop calling `poll_once()` on a schedule | implemented, unit tested |
| Budget governor (windows, ladder, reserve-then-settle) | implemented, unit tested |
| Workspace (fetch, checkout, merge-base diff, teardown) | implemented, unit tested |
| Budget layer 2 (path exclusions, pre-flight estimate) | implemented, unit tested |
| Engine seam (`ReviewEngine`, `Capabilities`, `FakeEngine`) | implemented, unit tested |
| Review worker (claim → run → settle), and its supervisor | implemented, unit tested |
| Engine adapter (`CliEngine` + `ClaudeCliEngine`) | implemented, unit tested, wired |
| Publisher | not started |
| Retention sweep | not started |

The ordering is deliberate: the **budget governor lands before the review
worker**, so the spending rails exist before anything can spend.

## 🧭 Next

1. **The budget pieces that still need a running engine:** the circuit
   breaker, layer 3's per-run enforcement and the ladder's 60 % rung. The
   worker has landed and settled the question the adapter named and could not
   answer — a review killed on its wall clock leaves tokens spent and no
   envelope to measure them, so it settles at its full reservation with
   `unavailable` confidence. See [WORKER.md](WORKER.md#-what-a-failed-run-settles-at).
2. **Publisher.** One line-anchored review, event `COMMENT`, with the
   `head_sha` re-check immediately before posting. It is also the consumer
   `ReviewResult.outcome` is waiting for: findings are publishable only from a
   run that completed.
3. **Retention sweep.** Purge content on merge; keep the ledger.

The queue now drains, and **the agent can spend.** The
[review worker](WORKER.md) claims through the [governor](BUDGET.md), checks
the pull request out, runs the configured adapter and settles the ledger.
"Nothing can spend" has stopped being structural and is now what the rails
enforce: the windows, the ladder, the pre-flight estimate, `max_run_tokens`
and `worker.count`. `CLAUDE.md` §5's rule — nothing reaches an engine outside
the governor — is doing work rather than describing a property the code had
for free, and a test pins it.

What is missing is anywhere to put the result: findings are logged and
dropped until the publisher lands.

A second engine plus a shared conformance suite is deliberately last: the
seam is worth defining early and filling late. It will be another CLI —
`codex` or `opencode` — because [every adapter is a
subprocess](DESIGN.md#-generalisation-to-other-agents) and no vendor SDK is
linked.

## ✅ Acceptance criteria

- [ ] **Feature is accessible:** the agent posts a line-anchored review on a
      freshly opened pull request from an allowlisted author, and on an
      allowlisted maintainer's `@claude` comment, with a 👀 acknowledgement
      within 15 s of the trigger.
- [ ] **A clean review** posts a single "no issues found" comment, edited in
      place rather than duplicated on re-review.
- [ ] **Triggers are correctly scoped:** no review on pushes to an existing
      pull request, no auto-review of an unlisted contributor's pull request,
      no trigger from a non-allowlisted account, no self-reply loop, no
      duplicate review for an unchanged `head_sha`, and no trigger from
      `@claude` inside a code fence or a blockquote.
- [ ] **Budget limits are enforced:** a synthetic concurrent load cannot breach
      any configured window (**done**); daily pacing prevents the weekly
      allowance being consumed in one day (**done**); the degradation ladder is
      observed at 85 / 100 % (**done**) and at 60 % (with the engine);
      `per_contributor_pct` bounds one contributor's share of the week
      (**done**); path exclusions keep a vendored-only change from being
      refused on size, and a pre-flight estimate over `max_run_tokens`
      refuses a run and releases its reservation (**done**); per-run ceilings
      terminate an over-budget review (with the engine).
- [ ] **Plan lockout is prevented:** with `reviewer_share_pct` configured,
      agent usage never exceeds its share of the session or weekly window
      (**done**), and a usage-limit error trips the breaker and decays the
      calibrated estimate (with the engine).
- [ ] `budget.enabled: false` takes effect without a restart (**done**);
      `publish.dry_run: true` does too, with the publisher.
- [ ] **Every posted comment is traceable** to a ledger row recording engine,
      model, mode, token usage and `usage_confidence`.
- [ ] **Operates entirely outbound:** no inbound port opened on the host,
      verified end to end from the target server.
- [ ] **Retention behaves as specified:** review content is purged after a pull
      request merges, ledger metrics survive the purge, and the `max_age_days`
      backstop fires for long-open pull requests.
- [ ] **Test coverage:** unit tests for mention parsing (fenced code, inline
      code, blockquotes), allowlist id-vs-login matching, dedupe key generation
      per trigger kind, rolling-window arithmetic, and the reserve-then-settle
      governor under concurrency; integration tests against recorded GitHub API
      fixtures; an engine conformance suite run against every adapter using a
      fixture pull request with seeded defects (the suite exists in
      `tests/test_engine.py`; it runs against `FakeEngine` alone until there
      is an adapter to add).
- [ ] **Documentation:** configuration reference, deployment and firewall
      prerequisites, billing-mode guidance, and an operator runbook covering the
      kill switch and the retention policy.

The first three lines of the test-coverage item, and the trigger-scoping
criterion, are met today by `tests/test_mention.py`,
`tests/test_allowlist.py` and `tests/test_classifier.py`.

## 🔭 Known gaps

Small things that are known and not yet done, so they are not rediscovered as
bugs:

- `strip_non_prose` normalises CRLF and drops a trailing newline, because it
  round-trips through `str.splitlines()`. Cosmetic for mention detection, which
  is its only caller today; it would matter if the function were reused for
  anything position-sensitive.
- The primary GitHub rate limit carries `x-ratelimit-reset` but no
  `Retry-After`, so the client raises rather than sleeping to the reset. See
  [POLLER.md](POLLER.md#-rate-limits-and-retries).
- A claimed trigger's `head_sha` is the head seen at classification time, and
  is `None` for a mention. The worker resolves it from `GET /pulls/{n}` when
  it claims, and reviews the head that read reported. **Re-checking it against
  the live head immediately before posting is still the publisher's**, and
  until that exists a review of a superseded commit would be published — see
  [QUEUE.md](QUEUE.md#-what-the-queue-does-not-do).
- **Nothing validates the configured token limits.** They are the operator's
  guess at a quota the plan does not publish, and until the circuit breaker
  lands the governor will report healthy utilisation while the real limit is
  being hit. Set them conservatively low. See
  [BUDGET.md](BUDGET.md#-not-built-yet).
- `claim()`'s `admit` hook is optional, so "nothing spends outside the
  governor" is held by a test rather than by the type system:
  `tests/test_worker.py::test_a_claim_is_taken_only_through_the_governor`
  asserts the hook the worker passes. The worker is the only caller that
  claims.
- `worker.count` needs a restart to take effect. `SIGHUP` swaps only the
  budget section, and the workers are built once at startup.
