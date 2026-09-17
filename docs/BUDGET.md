# Budget governor

**Not built yet.** This is the specification the implementation will be held
to, and the reason the phase order puts it before the review worker.

Because billing is subscription mode, the governor is not optional. All Claude
surfaces share one usage pool, so an unbounded reviewer does not merely
overspend — it locks the host operator out of their own interactive Claude Code
sessions until the window resets.

## 🪜 Five layers, cheapest first

| Layer | Mechanism | Cost to enforce |
| :-- | :-- | :-- |
| 1 | Allowlist, bot filter, draft skip, per-PR lifetime run cap, cooldown | zero tokens |
| 2 | Path exclusions (lockfiles, generated, vendored, minified), diff-size caps, ledger-fitted pre-flight token estimate | zero tokens |
| 3 | Per-run ceiling: max tokens (per-turn abort hook), max turns, wall-clock timeout | runtime-enforced |
| 4 | Rolling 5-hour and weekly plan windows, plus daily pacing and a per-contributor cap, enforced by reserve-then-settle admission control | own ledger |
| 5 | Degradation ladder rather than a hard stop | own ledger |

Layer 1 is [already implemented](TRIGGERS.md) — the classifier *is* the first
budget layer, which is why its rejections are logged at a level an operator
actually sees.

## 🧍 Human headroom

The agent is capped at a configurable *share* of each plan window
(`reviewer_share_pct`, default 40 %), never the whole allowance. A runaway
agent can degrade interactive Claude Code but cannot lock a maintainer out of
it.

This is the direct answer to "weekly threshold, no overspending": the agent's
ceiling is deliberately below the plan's.

## 📆 Pacing

A weekly cap alone permits burning the allowance on Monday. The effective
ceiling for any run is the **tightest** of:

- the session (rolling 5-hour) allowance;
- the weekly allowance;
- a daily allowance computed as `weekly_remaining ÷ days_remaining`;
- the per-contributor cap.

## 🔒 Reserve-then-settle

Checking the remaining allowance is not sufficient under concurrency. Three
workers can each observe sufficient budget, each start a run, and collectively
breach the cap — while every one of them checked correctly.

So the governor:

1. **reserves** the per-run maximum inside the *same* `BEGIN IMMEDIATE`
   transaction as the queue claim;
2. **runs**;
3. **settles** against reported usage and releases the remainder.

Reservations expire with the lease, so a crashed worker does not leak
allowance.

**This invariant is the main reason to prefer SQLite.** Its single write lock
makes the reservation atomic with the dequeue for free, where PostgreSQL would
need explicit row locking and a broker-plus-store split would need a
distributed transaction. See [STORAGE.md](STORAGE.md#-why-sqlite).

## 🔌 Self-calibrating circuit breaker

Because the plan's true limit is not published (constraint 4 in
[DESIGN.md](DESIGN.md#-the-four-constraints)), every configured limit is an
estimate.

Any usage-limit error trips a breaker until the window resets **and** decays
the calibrated estimate multiplicatively, so the system converges *downward*
onto the real limit rather than repeatedly hitting it.

## 📉 The degradation ladder

A hard stop at 100 % is the wrong shape — it makes the agent useless for the
rest of the window with no warning. Instead:

| Threshold of the tightest window | Behaviour |
| :-- | :-- |
| 60 % | drop to correctness and security dimensions only |
| 85 % | stop auto-reviewing fresh pull requests; still honour an explicit `@claude` |
| 100 % | post a notice naming the exhausted window and its reset time |

The mode is recorded on every ledger row, so a thinner-than-usual review is
explainable afterwards.

## 🧾 The ledger

Every posted comment must be traceable to a ledger row recording engine, model,
mode, token usage and `usage_confidence` (`exact` / `estimated` /
`unavailable`). That last field exists because
[some engines report no token usage at all](DESIGN.md#-generalisation-to-other-agents),
which forces the governor onto proxy controls — run-count, wall-clock and turn
caps — and that is a materially weaker guarantee an operator should be able to
see.

Ledger rows are never deleted, even when review content is purged: the rolling
windows are computed from historical usage.

## 🛑 Kill switches

All limits live in one `config.yaml`, reloadable on `SIGHUP`:

- `budget.enabled: false` is a hard kill switch;
- `publish.dry_run: true` exercises the full pipeline while posting nothing.

Both must take effect **without a restart**.

## 🧪 What the tests must pin

- A synthetic concurrent load cannot breach any configured window.
- Per-run ceilings terminate an over-budget review.
- The degradation ladder is observed at 60 / 85 / 100 %.
- Daily pacing prevents the weekly allowance being consumed in one day.
- With `reviewer_share_pct` configured, agent usage never exceeds its share of
  the session or weekly window.
- A usage-limit error trips the breaker and decays the calibrated estimate.
