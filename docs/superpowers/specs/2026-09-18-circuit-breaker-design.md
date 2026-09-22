# The circuit breaker: converge onto the limit nobody publishes

Design for issue
[#20](https://github.com/prasadtalasila/pr-review-agent/issues/20).

Issue #20 named
[#12](https://github.com/prasadtalasila/pr-review-agent/issues/12) as blocking,
because under `api_key` billing there is no unpublished limit to converge on
and decay would be dropped entirely. #12 is **closed**: subscription mode is
retained and the breaker is to be built as
[BUDGET.md](../../BUDGET.md) specifies. So decay stays in scope and every
limit stays token-denominated.

This change touches the spending rails. `CLAUDE.md` §5 applies in full.

## The problem

Every token limit the governor enforces is the operator's guess at a quota the
subscription never publishes. Guessing low is harmless. Guessing high is
invisible: the governor admits runs and reports healthy utilisation while the
real limit is already being hit, and
[the worker](../../WORKER.md) retries into the same wall until `max_attempts`
runs out — each retry spending. The maintainer is locked out of their own
interactive Claude Code sessions, and every control reports green because they
all measure the wrong thing.

Nothing detects this today. A usage-limit failure reaches
`ClaudeCliEngine._outcome` and folds into `Outcome.FAILED`, indistinguishable
from any other engine failure. [CONFIG.md](../../CONFIG.md) mitigates it with
advice — *set the limits conservatively low* — and advice is not a control.

## Scope

In: detection in the adapter, the trip and the calibration in the governor,
the `budget_state` table, the worker arm that joins them, and the tests that
pin all of it.

Out: layer 3's per-run ceilings
([#19](https://github.com/prasadtalasila/pr-review-agent/issues/19)), the
ladder's 60 % rung, and the publisher.

**No new configuration keys.** Every constant below is module-level in
`budget.py`. An operator's existing escape hatches — `session_tokens`,
`weekly_tokens`, `enabled` — already cover what they need to say, and
[CONFIG.md](../../CONFIG.md#-the-rule-the-loader-follows)'s rule is that a
knob nobody has asked for is the failure mode.

## ⚠️ What this design does not know

**Issue #20's first acceptance item is deliberately unmet.** It requires the
real usage-limit failure mode to be observed and documented before the
interface is fixed. It has not been observed: manufacturing one means driving
a live Max subscription into its wall, which is exactly the lockout
[constraint 3](../../DESIGN.md#-the-four-constraints) exists to prevent, and
is not this change's to spend.

So detection is designed to be **cheap to correct**. Everything the guess
touches is one constant, `_USAGE_LIMIT_MARKERS` in
`src/pr_review_agent/engine/claude.py`. When the real error is seen, editing
that tuple is the whole fix — no signature changes, no migration, no
redesign. Both plausible carriers are covered so the guess has two chances to
be right:

- the CLI exits nonzero and says so on stderr, or
- the CLI prints a result envelope whose `subtype` or error text says so.

`trip(window, resets_at)`, the signature BUDGET.md sketched, is **not** built.
[Constraint 4](../../DESIGN.md#-the-four-constraints) says the plan exposes no
reset time, so a reset time cannot be a parameter. `trip(now)` takes a
duration instead — see below.

## Design

### Detect

`UsageLimited(EngineError)` joins `EngineTimeout` and `EngineUnavailable` in
`engine/cli.py`. It carries an optional `Usage`, because a usage limit fails
in two shapes and they differ in what the run cost:

- **Refused up front.** The CLI exits nonzero having done no work. Spend is
  *known to be zero*, exactly as in `Governor.preflight`'s refusal. `usage`
  is `None` and the worker settles at `Usage(0, EXACT)`.
- **Hit mid-run.** The CLI prints a result envelope, and that envelope carries
  its own `usage`. Spend is *measured*. The worker settles at that figure.

This is not an exemption from
[settles at what is knowable](../../BUDGET.md#a-caught-failure-settles-at-what-is-knowable).
That rule charges the full reservation because a killed run's spend is
unknowable; a usage-limit refusal is one of the few failures where it **is**
knowable, in both shapes. Charging the ceiling instead would write tokens that
were never spent into all three rolling windows, where — the windows being
rolling and ledger rows never deleted — they would keep refusing runs for up
to seven days after the real quota had cleared.

`CliEngine` gains one overridable predicate, `usage_limited(text) -> bool`,
defaulting to `False`. `CliEngine.run` consults it on a nonzero exit before
raising `EngineProtocolError`; `ClaudeCliEngine.parse` consults it on the
envelope. One hook, two call sites, no framework.

### Trip

`Governor.trip(now)` records `tripped_until = now + SESSION` and
`last_trip_at = now`. `admit()` refuses every claim while `tripped_until` is
in the future, ahead of the headroom check and immediately after the
`enabled` check — a tripped breaker is a fact about the account, so it
outranks arithmetic about windows.

Five hours because `SESSION` is the shortest window, and because it is a
**duration rather than a reset time**, which is the only kind of answer
available when the plan publishes none. If the weekly limit was the one that
blew, the next attempt trips again and the calibration keeps shrinking; the
design converges either way rather than needing the attribution to be right.

A refused claim costs no attempt. `ReviewQueue.claim` already skips a
candidate its `admit` hook rejects rather than burning one, for the reason its
docstring gives: a refusal is about the allowance, not about the trigger.

### Decay, and recovery

The same trip multiplies a stored calibration by `DECAY_FACTOR` (0.9). Every
window limit becomes `configured * calibrated_pct // 100`, so the ceiling
drops a tenth each time reality disagrees with the operator's guess, and
converges downward over a handful of windows instead of hitting the wall once
per window forever.

Recovery is additive: `+1` percentage point per clean `SESSION` since the last
trip, capped at 100. Multiplicative down and additive up is what makes the
series converge rather than oscillate, and it is what stops a **one-off** heavy
interactive week from crippling the reviewer permanently — the pool is shared
(`DESIGN.md` constraint 3), so a trip does not always mean the guess was too
high. A calibration that could only ever be revised downward would be the
mirror of the permanent floor
[BUDGET.md rejected](../../BUDGET.md#the-cold-start-errs-high) under the
tokens-per-line fit.

Because a trip holds for a full `SESSION` and recovery accrues only in clean
`SESSION`s, decay is **self-rate-limiting**: no second timer, no separate gate.

Recovery is computed on read, as a pure function of
`(stored_pct, last_trip_at, now)` — nothing is written on a read path.

The calibration is an **integer percentage floored at 1**, never a float.
`_headroom` divides by `window.limit`, so a calibration reaching zero is a
crash rather than a policy; `BudgetConfig` already validates
`reviewer_share_pct` as `1 <= pct <= 100` for exactly that reason, and this
follows the same rule. Integers also mean no float drift across restarts.

### State

Migration 6, `SCHEMA_VERSION` 5 → 6:

```sql
CREATE TABLE IF NOT EXISTS budget_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Three keys: `calibrated_pct`, `tripped_until`, `last_trip_at`. Key/value
rather than columns because these are three unrelated scalars, not a row of
one thing. Absent keys mean *never tripped*, so an existing database needs no
backfill.

It is a separate table from `ledger` on purpose. Ledger rows are tokens
genuinely consumed and the rolling windows sum them; writing breaker state
there would corrupt the one invariant
[BUDGET.md](../../BUDGET.md#-the-ledger) is strictest about.

### Join

`worker.py` gains one arm ahead of the generic `EngineError` one: call
`governor.trip(now)`, settle at the carried usage or `Usage(0, EXACT)`, and
release the row. `_review` must let `UsageLimited` through rather than
wrapping it — it currently catches bare `Exception` and converts everything
into the worker's own `EngineError`.

Release rather than abandon: the pull request still deserves a review once the
account recovers, and the breaker — not the queue — is what stops it being
attempted meanwhile.

## What the tests pin

`tests/test_budget.py`

- a trip refuses admission, and the refusal costs no attempt;
- admission resumes once `SESSION` has passed;
- a trip decays the calibration, and the effective limit is
  `configured * calibrated // 100`;
- the calibration floors at 1 rather than reaching 0;
- recovery accrues one point per clean `SESSION` and caps at 100;
- **convergence:** repeated trips drive the calibration monotonically
  non-increasing — the "downward, not oscillation" criterion — and it recovers
  once trips stop;
- **restart:** a second `Governor` over the same store reads the calibration
  back.

`tests/test_cli_engine.py`

- a nonzero exit whose stderr matches raises `UsageLimited` with no usage;
- an envelope that matches raises `UsageLimited` carrying the envelope's
  usage;
- an unrelated failure still raises `EngineProtocolError`.

`tests/test_worker.py`

- a `UsageLimited` run trips the governor, settles at the known figure rather
  than the ceiling, and leaves the row pending.

`tests/test_store.py`: migration 6 applies and `SCHEMA_VERSION` is 6.

## Documentation

No new page: [BUDGET.md](../../BUDGET.md) grows from *the budget governor*
into the token-management page. The "🪜 Five layers" table gains the controls
that live outside the governor (`worker.count`, the engine wall-clock timeout,
the cold-start watermark) with links out, and the breaker moves from
"🕳 Not built yet" into a section of its own.

Then [CONFIG.md](../../CONFIG.md)'s "until the breaker lands" advice,
[STORAGE.md](../../STORAGE.md)'s schema, [WORKER.md](../../WORKER.md)'s
failure taxonomy, [ENGINE.md](../../ENGINE.md)'s "what lands next", and
[STATUS.md](../../STATUS.md)'s known gaps and acceptance lines.
