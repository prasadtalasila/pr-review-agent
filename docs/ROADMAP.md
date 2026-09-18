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
| Engine adapter (`ReviewEngine`) | not started |
| Publisher | not started |
| Retention sweep | not started |

The ordering is deliberate: the **budget governor lands before the review
worker**, so the spending rails exist before anything can spend.

## 🧭 Next

1. **Engine adapter and publisher.** One line-anchored review, event `COMMENT`,
   with the `head_sha` re-check immediately before posting. It also carries the
   budget pieces that need a running engine: the circuit breaker, layer 3's
   per-turn enforcement, the ladder's 60 % rung, and the rest of layer 2 —
   path exclusions and the pre-flight token estimate. Layer 2's diff-size
   caps already landed with the [workspace](WORKSPACE.md), which is the first
   thing that needed them. The code to review is now on disk for it.
2. **Retention sweep.** Purge content on merge; keep the ledger.

The [daemon loop](DAEMON.md) fills the queue and nothing drains it, which is
still the intended state: the backlog is visible and none of it has cost
anything. The [budget governor](BUDGET.md) is now in place ahead of the
worker, so the spending rails exist before anything can spend, and the
[workspace](WORKSPACE.md) can put a pull request on disk without anything
yet reading it.

A second engine (PR-Agent via `pr_agent_litellm`) plus a shared conformance
suite is deliberately last: the seam is worth defining early and filling late.

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
      observed at 85 / 100 % (**done**) and at 60 % (with the engine); per-run
      ceilings terminate an over-budget review (with the engine).
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
      fixture pull request with seeded defects.
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
  is `None` for a mention. Resolving it, and re-checking it against the live
  head before posting, belongs to the publisher — see
  [QUEUE.md](QUEUE.md#-what-the-queue-does-not-do).
- **Nothing validates the configured token limits.** They are the operator's
  guess at a quota the plan does not publish, and until the circuit breaker
  lands the governor will report healthy utilisation while the real limit is
  being hit. Set them conservatively low. See
  [BUDGET.md](BUDGET.md#-not-built-yet).
- **No per-contributor cap.** It would ship inert against a one-person
  allowlist, so it is deferred; `actor_id` is recorded on every ledger row
  meanwhile, because an append-only ledger cannot backfill attribution.
- `claim()`'s `admit` hook is optional, so "nothing spends outside the
  governor" is held by review rather than by the type system. The only caller
  that will ever claim is the worker the engine phase adds.
