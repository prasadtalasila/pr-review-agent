# Budget governor

**Implemented** in `src/pr_review_agent/budget.py`, over the `ledger` table
declared in [STORAGE.md](STORAGE.md#-schema). The design decisions behind it,
including what was cut and why, are recorded in
[the design note](superpowers/specs/2026-09-17-budget-governor-design.md).

Because billing is subscription mode, the governor is not optional. All Claude
surfaces share one usage pool, so an unbounded reviewer does not merely
overspend — it locks the host operator out of their own interactive Claude Code
sessions until the window resets. That is why it lands **before** the review
worker: the spending rails exist before anything can spend.

## 🪜 Five layers, cheapest first

| Layer | Mechanism | State |
| :-- | :-- | :-- |
| 1 | Allowlist, bot filter, draft skip, cold-start watermark | done — [TRIGGERS.md](TRIGGERS.md) |
| 2 | Path exclusions, diff-size caps, pre-flight token estimate | with the engine adapter |
| 3 | Per-run ceiling: max tokens, max turns, wall-clock timeout | `max_run_tokens` done; enforcement with the engine |
| 4 | Rolling windows and pacing, by reserve-then-settle | **done** |
| 5 | A degradation ladder rather than a hard stop | **done** |

Layer 1 is the classifier — it *is* the first budget layer, which is why its
rejections are logged at a level an operator actually sees.

Layers 2 and 3 need a diff in hand and a running turn to abort, so they belong
to the phase that has both. `max_run_tokens` lands here because the governor
reserves against it; `max_turns` and `wall_clock_seconds` do not, because
nothing would read them and
[CONFIG.md](CONFIG.md#-the-rule-the-loader-follows)'s rule is that a setting
which does nothing is exactly the failure to avoid.

## 🧍 Human headroom

The agent is capped at a configurable *share* of each plan window
(`reviewer_share_pct`, default 40 %), never the whole allowance. A runaway
agent can degrade interactive Claude Code but cannot lock a maintainer out of
it.

This is the direct answer to "weekly threshold, no overspending": the agent's
ceiling is deliberately below the plan's.

## 📆 Three windows, one shape

Every limit is tokens recorded in the ledger within a trailing duration. The
effective ceiling for a run is the **tightest** of the three; the ladder rung
comes from the **worst** utilisation among them.

| Window | Duration | Limit |
| :-- | :-- | :-- |
| session | 5 h | `session_tokens × share` |
| weekly | 7 d | `weekly_tokens × share` |
| daily | 24 h | `weekly_tokens × share ÷ 7` |

A weekly cap alone would permit burning the allowance on Monday, which is what
the daily window prevents: **at most a seventh of the week in any day.**

That is a deliberate reading of a gap in the original specification, which
paced the day as `weekly_remaining ÷ days_remaining`. A rolling weekly window
never resets, so `days_remaining` has no value to take. A flat seventh needs no
week anchor — one the plan does not publish and we would have had to invent —
and it is *stricter*: an agent idle since Monday cannot burn four days'
allowance on Friday. Unspent allowance is not a loss here. It is headroom for
humans, which is the point.

## 🔒 Reserve-then-settle

Checking the remaining allowance is not sufficient under concurrency. Three
workers can each observe sufficient budget, each start a run, and collectively
breach the cap — while every one of them checked correctly.

So the governor:

1. **reserves** the per-run maximum inside the *same* `BEGIN IMMEDIATE`
   transaction as the [queue claim](QUEUE.md#-the-admit-hook), through
   `claim()`'s `admit` hook;
2. **runs**;
3. **settles** against reported usage and releases the remainder.

**This invariant is the main reason to prefer SQLite.** Its single write lock
makes the reservation atomic with the dequeue for free, where PostgreSQL would
need explicit row locking and a broker-plus-store split would need a
distributed transaction. See [STORAGE.md](STORAGE.md#-why-sqlite).

One query measures a window, and it is the whole guarantee:

```sql
SELECT COALESCE(SUM(COALESCE(used_tokens, reserved_tokens)), 0)
FROM ledger WHERE reserved_at > :start
```

A settled row counts what it actually spent; an unsettled one counts its full
reservation. The second worker therefore sees the first's reservation as
already spent, before the first has finished.

`tests/test_budget_concurrency.py` pins this with eight real threads over one
database file.

### A crashed worker's reservation stays charged

An unsettled reservation keeps counting until it ages out of its rolling
window. It is never released early, and there is no expiry sweep.

The original specification said a reservation expires with the lease, "so a
crashed worker does not leak allowance". This honours the intent rather than
the letter. A crashed run has probably already spent tokens, and
`queue.DEFAULT_MAX_ATTEMPTS` is 3, so releasing on expiry would let one trigger
spend invisibly three times over. *Leaking* means held forever, which a rolling
window already rules out; it does not mean granting an amnesty. For a spending
control the pessimistic direction is the safe one.

The happy consequence: the ledger needs no `expires_at`, no `state` column and
no sweeper.

## 📉 The degradation ladder

A hard stop at 100 % is the wrong shape — it makes the agent useless for the
rest of the window with no warning. Instead:

| Worst utilisation | Mode | Behaviour |
| :-- | :-- | :-- |
| < 85 % | `full` | admit |
| ≥ 85 % | `mention_only` | stop auto-reviewing fresh pull requests; still honour an explicit `@claude` |
| ≥ 100 % | `exhausted` | refuse everything |

A run is also refused, at any rung, when the tightest window's remainder is
below `max_run_tokens`. There is no partial reservation: a run that cannot be
afforded in full is not started, because a truncated review is still a spend.

The mode is recorded on every ledger row, so a thinner-than-usual review is
explainable afterwards.

In practice `exhausted` is reached only by an **overrun** — a run that settles
above what it reserved. Admission alone stalls just short of 100 %, because the
must-fit-whole rule refuses before the window fills. That is exactly the case a
hard stop exists to cover.

The 60 % rung of the original ladder — drop to correctness and security
dimensions only — is **not** implemented. There is no engine to thin a review,
so it would be a mode recorded and never acted on. It returns with the engine
adapter.

## 🧾 The ledger

Every posted comment must be traceable to a ledger row recording engine, model,
mode, token usage and `usage_confidence` (`exact` / `estimated` /
`unavailable`). That last field exists because
[some engines report no token usage at all](DESIGN.md#-generalisation-to-other-agents),
which forces the governor onto proxy controls — run-count, wall-clock and turn
caps — and that is a materially weaker guarantee an operator should be able to
see.

Ledger rows are **never deleted**, even when review content is purged: the
rolling windows are computed from historical usage. Deleting one would silently
hand back allowance that was genuinely spent.

`engine` and `model` are `NULL` between reserve and settle, because nothing
knows them until a run finishes. `actor_id` is recorded on every row but
nothing enforces against it yet — see below.

## 🛑 Kill switches

All limits live in one `config.yaml`, reloaded on `SIGHUP`:

- **`budget.enabled: false` stops reviewing.** It does *not* turn the budget
  checks off. The two readings of "kill switch" differ by catastrophe, so it is
  worth being explicit: the governor refuses every claim while it is false.
- `publish.dry_run: true` exercises the full pipeline while posting nothing.
  It arrives with the publisher; the reload mechanism built here takes it
  without change.

`SIGHUP` re-reads the file and swaps only the `budget` section. A changed
repository or store path is logged as needing a restart rather than
half-applied, and **a broken file leaves the previous configuration in force** —
crashing would turn the emergency brake into a way to take the service down
with a typo.

## 🕳 Not built yet

Recorded here so they are not rediscovered as omissions.

**The circuit breaker and calibration decay.** Every limit above is the
operator's guess, because the plan publishes no quota. A guess that is too low
is harmless. A guess that is too high is the one failure the rest of this design
cannot see: the governor reports healthy utilisation while the real limit is
being hit. The breaker is the only feedback from reality into that guess, and
multiplicative decay is what makes it converge downward instead of hitting the
wall once per window forever.

It is deferred because its interface is determined by an error nobody has seen
yet. `trip(window, resets_at)` assumes the failure names which window blew and
when it resets, and [constraint 4](DESIGN.md#-the-four-constraints) says the
plan exposes no reset time. Designing it against a synthetic call, then
rewriting it once the engine adapter shows what a real usage-limit error looks
like, is worse than designing it once alongside the detector.

Nothing drains the queue today, so no run can hit a limit and there is no
exposure. The cost is operator guidance: **set `session_tokens` and
`weekly_tokens` conservatively low until the breaker lands**, because nothing
will catch an over-estimate.

**The per-contributor cap.** The allowlist holds one person, so any cap below
100 % would block the only account that can trigger anything — it would ship
inert. `actor_id` is on every ledger row regardless, because the ledger is
append-only and attribution is the one field that cannot be backfilled.

## 🧪 What the tests pin

In `tests/test_budget.py`, `tests/test_budget_concurrency.py` and the `admit`
cases in `tests/test_queue.py`:

- a synthetic concurrent load cannot breach any configured window;
- with `reviewer_share_pct` configured, agent usage never exceeds its share;
- daily pacing refuses a run the weekly window alone would have allowed;
- the ladder is observed at 85 % and 100 %, and at 85 % a refused pull request
  does **not** block a maintainer's `@claude` behind it in the queue;
- a refusal costs no attempt and leaves the row `pending`;
- an unsettled reservation counts in full, and keeps counting past its lease;
- `settle` releases the remainder and records engine, model and confidence;
- `budget.enabled: false` admits nothing, and `SIGHUP` flips it without a
  restart;
- a `SIGHUP` against an unparseable file keeps the previous config in force.

Per-run ceilings terminating an over-budget review is layer 3, and lands with
the engine adapter.
