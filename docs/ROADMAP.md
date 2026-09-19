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
| Publisher (👀, head re-check, one comment per PR) | implemented, unit tested |
| Circuit breaker and calibration decay | implemented, unit tested |
| Retention sweep | not started |

The ordering is deliberate: the **budget governor lands before the review
worker**, so the spending rails exist before anything can spend.

## 🧭 Next

1. **The last budget piece that needs a running engine:** the ladder's 60 %
   rung. The [circuit breaker](BUDGET.md#-the-circuit-breaker) has landed, so
   an over-estimated limit is no longer invisible: a usage-limit failure
   refuses every claim for five hours and decays the effective limits toward
   the real one. What it has **not** had is a sighting of the real error — see
   the known gaps. Layer 3's wall clock has landed too, validated below the
   queue lease; its *token* ceiling did not, because every way to build one
   assumes an engine that reports tokens, and it belonged with the breaker.
   See [BUDGET.md](BUDGET.md#where-layer-3s-three-ceilings-ended-up).
2. **Retention sweep.** Purge content on merge; keep the ledger. It is
   specified in terms of the `runs` table the publisher added, and
   `RunStore.purge_content` is already there waiting for a caller.

The queue now drains, and **the agent can spend.** The
[review worker](WORKER.md) claims through the [governor](BUDGET.md), checks
the pull request out, runs the configured adapter and settles the ledger.
"Nothing can spend" has stopped being structural and is now what the rails
enforce: the windows, the ladder, the pre-flight estimate, `max_run_tokens`
and `worker.count`. `CLAUDE.md` §5's rule — nothing reaches an engine outside
the governor — is doing work rather than describing a property the code had
for free, and a test pins it.

The [publisher](PUBLISHER.md) now makes the result visible: a 👀 at claim
time, a live `head_sha` re-check, and one comment per pull request edited in
place on re-review. It can make no other kind of write, which is how
[DESIGN.md](DESIGN.md#-prompt-injection-is-in-scope)'s third mitigation stops
being a promise about code and becomes a property of it.

What is missing is anywhere for a finished pull request's content to go:
review bodies accumulate in `runs` until the retention sweep lands.

A second engine plus a shared conformance suite is deliberately last: the
seam is worth defining early and filling late. It will be another CLI —
`codex` or `opencode` — because [every adapter is a
subprocess](DESIGN.md#-generalisation-to-other-agents) and no vendor SDK is
linked.

## ✅ Acceptance criteria

- [x] **Feature is accessible:** the agent posts a review comment on a freshly
      opened pull request from an allowlisted author, and on an allowlisted
      maintainer's `@claude` comment, with a 👀 acknowledgement within 15 s of
      the trigger **being seen**. The original criterion said "a line-anchored
      review ... within 15 s of the trigger"; both halves were reworded when
      the publisher landed. Findings are posted in one comment rather than
      inline, because inline comments require the reviews endpoint and with it
      an `event` field — see
      [PUBLISHER.md](PUBLISHER.md#-the-publisher-cannot-approve-anything). And
      the poll interval is 10–600 s, so no acknowledgement can be within 15 s
      of a comment being *written*; the 👀 is what makes that latency
      imperceptible, which is the reason it was specified.
- [x] **A clean review** posts a single "no issues found" comment, edited in
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
      refuses a run and releases its reservation (**done**); a review wall
      clock above the queue lease fails startup (**done**); per-run ceilings
      terminate an over-*budget* review (with the breaker).
- [ ] **Plan lockout is prevented:** with `reviewer_share_pct` configured,
      agent usage never exceeds its share of the session or weekly window
      (**done**), and a usage-limit error trips the breaker and decays the
      calibrated estimate (**done** — against a detector whose markers are
      still unconfirmed; see the known gaps).
- [x] `budget.enabled: false` takes effect without a restart, and so does
      `publish.dry_run: true`.
- [x] **Every posted comment is traceable** to a ledger row recording engine,
      model, mode, token usage and `usage_confidence`: `runs.dedupe_key` joins
      the queue row and the ledger rows that paid for it.
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
- **A comment GitHub will never accept is retried on every claim for that
  pull request.** A publish-only retry deliberately does not count against
  `max_attempts` — the bound measures allowance drained, and a post that
  reaches no engine drains none — so nothing eventually gives up on it. The
  retention sweep is where this stops mattering: a purged run is no longer
  offered for publication.
- **Review content accumulates in `runs` and nothing purges it yet.**
  `RunStore.purge_content` exists and has no caller; the retention sweep is
  the next component.
- **Nothing validates the configured token limits.** They are the operator's
  guess at a quota the plan does not publish. The
  [circuit breaker](BUDGET.md#-the-circuit-breaker) now catches an
  over-estimate and decays the effective limits toward the real one, but it
  only learns by hitting the wall — every trip is a lockout the operator
  could have avoided by guessing lower to begin with. Set them conservatively
  low. See [BUDGET.md](BUDGET.md#-not-built-yet).
- **Worktrees stranded by the relative-`cache_dir` bug are not cleaned up by
  code.** Before the fix, git wrote each run directory inside the mirror, and
  `git worktree prune` will not remove a directory that still exists. An
  operator who ran an affected version deletes `.cache/repos/*.git/.cache/`
  by hand, once. It cannot recur now that the path is absolute, which is why
  there is no code for it.
- `claim()`'s `admit` hook is optional, so "nothing spends outside the
  governor" is held by a test rather than by the type system:
  `tests/test_worker.py::test_a_claim_is_taken_only_through_the_governor`
  asserts the hook the worker passes. The worker is the only caller that
  claims.
- `worker.count` needs a restart to take effect. `SIGHUP` swaps only the
  budget section, and the workers are built once at startup.
