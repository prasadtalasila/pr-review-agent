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
| 2 | Path exclusions, diff-size caps, pre-flight token estimate | **done** — [WORKSPACE.md](WORKSPACE.md) and below |
| 3 | Per-run ceiling: max tokens, max turns, wall-clock timeout | `max_run_tokens` done; enforcement with the engine |
| 4 | Rolling windows and pacing, by reserve-then-settle | **done** |
| 5 | A degradation ladder rather than a hard stop | **done** |

Layer 1 is the classifier — it *is* the first budget layer, which is why its
rejections are logged at a level an operator actually sees.

Layer 3 needs a *running turn* to abort, so it belongs to the phase that has
one. `max_run_tokens` lands here because the governor reserves against it;
`max_turns` and `wall_clock_seconds` do not, because nothing would read them
and [CONFIG.md](CONFIG.md#-the-rule-the-loader-follows)'s rule is that a
setting which does nothing is exactly the failure to avoid.

Layer 2 needs only a *diff*, which is why it lands before the engine rather
than with it.

**The diff-size caps arrived first**, with the [workspace](WORKSPACE.md).
`max_changed_files` and `max_changed_lines` both exist because neither bounds
the other — two thousand one-line files pass a line cap and still bury the
engine, and one fifty-thousand-line generated file passes a file cap.

They are in this section, rather than beside the code that reads them,
because every spending cap belongs in one place. Being here also makes them
reloadable on `SIGHUP`, which is why the workspace is handed them per
checkout rather than holding a snapshot.

Be clear about what they bound: **the engine's input, not the disk**. A
fetch pulls every object reachable from the head, so a commit that adds a
large blob and a later one that removes it still downloads it while
reporting no changed lines at all.

## 🚫 Path exclusions

```yaml
budget:
  excluded_paths:
    - '**/package-lock.json'
    - '**/vendor/**'
    - '**/*.min.js'
```

Lockfiles, vendored trees, generated code and minified bundles. Reviewing
them is close to worthless and they dominate diff size, which makes this the
largest saving available for zero tokens.

**Exclusions apply to the size caps and to the diff the engine is shown, and
they are the same list applied by the same mechanism.** Each pattern becomes
a git pathspec — `:(exclude,glob)<pattern>` — passed to both the
`git diff --numstat` the caps are measured on and the `git diff` that
produces `Checkout.diff`. They cannot drift apart, because there is only one
of them.

That second half holds by construction rather than by care:
[ENGINE.md](ENGINE.md)'s `ReviewRequest` carries the checkout and
deliberately does not repeat the diff beside it, so `Checkout.diff` is the
only diff in the system.

Both halves are needed. Applying exclusions to the engine alone would leave a
vendored-dependency bump refused on a size cap for lines the engine was never
going to see — a pull request rejected for content that does not exist.

Setting the key **replaces** the default list rather than adding to it, and
an empty list excludes nothing: a repository that genuinely reviews its
lockfiles is a real repository. A pattern may not begin with `:`, because the
pathspec magic is the agent's to supply and a pattern that rewrites its own
meaning is not something an operator can predict from reading their own
configuration file.

### The size gate moved to make this possible

It now runs **after** the fetch, on `--numstat`, rather than before it on the
API's totals. That is not a preference; `GET /pulls/{n}` reports three
aggregate integers with no per-path breakdown, and a lockfile cannot be
subtracted from an integer.

So "refused before anything is written to disk" is now "refused before a
worktree exists and before any diff can reach an engine". What was given up
is affordable: a fetch costs bandwidth and disk, and **these caps are a
spending control over tokens**. The section above already conceded that they
bound what the engine reads rather than what the fetch downloads — the
property surrendered was never the one doing the work.

The alternative was `GET /pulls/{n}/files`, which paginates to thirty
requests per claimed trigger, truncates at three thousand files, and is the
endpoint [WORKSPACE.md](WORKSPACE.md#-why-a-checkout-rather-than-the-api-diff)
already rejected for the diff.

A refusal logs both figures — what survived exclusion and what the API
reported. "Refused at 3 files" is baffling beside a pull request GitHub says
has 900; the gap between the two numbers *is* the explanation.

## 🔮 The pre-flight token estimate

The last refusal that costs nothing. `Governor.preflight` predicts a run's
cost and refuses it when the prediction exceeds `max_run_tokens` — or when
nothing is left to review after exclusions, which a lockfile-only pull
request now is.

```text
estimate = rate × reviewable lines
```

No fitted intercept. A review has a fixed overhead — the prompt, the
instructions, the first file read — but the only question asked here is
whether a pull request is *large* enough to refuse, and under-predicting a
fifty-line change is harmless because a fifty-line change is nowhere near the
cap. The fixed cost is amortised into the rate, where it makes large diffs
predict slightly high: the safe direction.

`rate` is fitted against the ledger — `Σ used_tokens ÷ Σ reviewed_lines` —
over settled rows whose `usage_confidence` is `exact`. Rows from an engine
that reported no usage are excluded, because fitting a rate to a run nobody
measured would turn [the known-weaker guarantee](#-the-ledger) into a
confidently wrong number.

`reviewed_lines` is the column migration 5 added, and it is written by
`settle` from `Checkout.reviewed` — **never by the engine.**
`ReviewResult.usage` *is* `budget.Usage`, so putting the field there would
make an adapter responsible for reporting the size it was handed, and an
adapter that under-reported would bias the rate downward. A spending control
must not take its input from the thing it controls.

### The cold start errs high

A fresh database has no rows to fit against, and the first runs are exactly
when an over-estimate is cheapest to get wrong. So **40 tokens per line until
ten fittable rows exist**, after which the fit takes over outright.

Forty is above what a review is expected to cost, which over-refuses rather
than overspending; against the shipped `max_run_tokens` it puts the threshold
at 1,500 reviewable lines. Neither number is configurable: an operator's
escape hatch is `max_run_tokens`, which they already have to choose, and a
second knob multiplying into it is CONFIG.md's failure mode.

Declining to estimate until data existed was the alternative, and it leaves
the least-calibrated moment unguarded — a run that truly costs 200,000
tokens against a 60,000 reservation completes, and the overrun surfaces at
settle, after the tokens are gone. That overrun is precisely what reaches
`exhausted`.

A permanent floor — `max(fitted, 40)` — was also rejected. After five hundred
runs proving reviews cost twelve tokens a line it would still refuse pull
requests the agent has direct evidence it can afford. A rate that can only be
revised upward is not a fit.

### One run is not one turn

Worth knowing before the fit is trusted too far. The `claude` adapter asks
for schema-constrained output, and the CLI **re-prompts by itself** when the
model's answer does not fit — see
[ENGINE.md](ENGINE.md#the-schema-retry-is-inside-the-cli-and-it-spends). That
retry is not something the adapter can disable or bound.

The ledger stays honest, because the envelope's usage covers every attempt.
The *fit* stays honest too, for the same reason: it is fitted against what
runs actually cost, retries included, so a corpus with a normal retry rate
predicts a normal retry rate.

What this does affect is variance. A run that hits the retry path costs a
multiple of one that does not, over the same reviewable lines, so the residual
around the fitted rate is wider than a line-count model suggests — and the
estimate is a refusal threshold rather than a reservation, which is the
reading that survives that variance. The reservation is still
`max_run_tokens`, and it is what the retry path is actually bounded by.

### Refusing releases the reservation

The reservation is taken by `admit`, inside the claim, before the pull
request's size is knowable — the facts read happens once per *claimed*
trigger, because reading it per open pull request per cycle is the design
[POLLER.md](POLLER.md) exists to refuse. So a pre-flight refusal cannot
happen before a reservation exists; what it must do instead is hand the
reservation straight back.

`preflight` settles the row at zero tokens **in the same call as the
refusal**. A caller that refused and forgot to settle would leave a full
reservation charged against every window until it aged out, because
[nothing releases a reservation early](#a-crashed-workers-reservation-stays-charged)
by design. Making the decision and the release one call means that failure
cannot be introduced by a caller.

A refused row records `used_tokens = 0` at confidence `exact` — the cost is
not unknown, it is known to be nothing — and leaves `reviewed_lines` NULL, so
a refusal never contributes to the fit.

## 🧍 Human headroom

The agent is capped at a configurable *share* of each plan window
(`reviewer_share_pct`, default 40 %), never the whole allowance. A runaway
agent can degrade interactive Claude Code but cannot lock a maintainer out of
it.

This is the direct answer to "weekly threshold, no overspending": the agent's
ceiling is deliberately below the plan's.

## 📆 Windows, one shape

Every limit is tokens recorded in the ledger within a trailing duration. The
effective ceiling for a run is the **tightest** of them; the ladder rung comes
from the **worst** utilisation among them.

| Window | Duration | Limit |
| :-- | :-- | :-- |
| session | 5 h | `session_tokens × share` |
| weekly | 7 d | `weekly_tokens × share` |
| daily | 24 h | `weekly_tokens × share ÷ 7` |
| contributor | 7 d | `weekly_tokens × share × per_contributor_pct` — only when configured |

A weekly cap alone would permit burning the allowance on Monday, which is what
the daily window prevents: **at most a seventh of the week in any day.**

That is a deliberate reading of a gap in the original specification, which
paced the day as `weekly_remaining ÷ days_remaining`. A rolling weekly window
never resets, so `days_remaining` has no value to take. A flat seventh needs no
week anchor — one the plan does not publish and we would have had to invent —
and it is *stricter*: an agent idle since Monday cannot burn four days'
allowance on Friday. Unspent allowance is not a loss here. It is headroom for
humans, which is the point.

**The contributor window is the only one scoped to a person.** It measures
what the claim's own `actor_id` has spent over the same rolling week, so it is
built per claim rather than per configuration, and it joins the list only when
`per_contributor_pct` is set — unset, the other three behave exactly as they
did before it existed. Being in the list means it degrades and refuses on the
ordinary ladder below, but only for the contributor being admitted: everyone
else's headroom is measured separately, which is the whole purpose. `headroom()`,
the operator readout, has no contributor to scope to and therefore reports the
three shared windows only.

The cap is **meaningless on a one-person allowlist** — the one account able to
trigger anything would simply meet its own cap. It earns its keep once several
people can trigger reviews and one monopolising the week is a real outcome.
`actor_id` has been on every ledger row since the governor shipped, precisely
so this stayed possible: the ledger is append-only, and attribution is the one
field that cannot be backfilled.

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

### A caught failure settles at what is knowable

A *lost* worker's reservation stays charged, as above. A worker that caught
an exception is not lost, and settles — but at what? The spend is unknowable
for an engine killed mid-run, so the rule splits on whether the engine had
started: a failure before it settles at zero, a failure in or after it
settles at the full reservation. See
[WORKER.md](WORKER.md#-what-a-failed-run-settles-at).

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


## 🧪 What the tests pin

In `tests/test_budget.py`, `tests/test_budget_concurrency.py` and the `admit`
cases in `tests/test_queue.py`:

- a synthetic concurrent load cannot breach any configured window;
- with `reviewer_share_pct` configured, agent usage never exceeds its share;
- daily pacing refuses a run the weekly window alone would have allowed;
- with `per_contributor_pct` unset nothing is scoped to a contributor, and
  with it set one contributor's second run is refused while another's is still
  admitted, the refusal naming the contributor window;
- the ladder is observed at 85 % and 100 %, and at 85 % a refused pull request
  does **not** block a maintainer's `@claude` behind it in the queue;
- a refusal costs no attempt and leaves the row `pending`;
- an unsettled reservation counts in full, and keeps counting past its lease;
- `settle` releases the remainder and records engine, model and confidence;
- `budget.enabled: false` admits nothing, and `SIGHUP` flips it without a
  restart;
- a `SIGHUP` against an unparseable file keeps the previous config in force;
- an empty ledger estimates at the documented constant and still refuses an
  oversized pull request, the fit takes over at the tenth fittable row, and
  rows the engine could not measure are not fitted;
- a pre-flight refusal returns the window to exactly where it was, and a
  pull request with nothing left to review is refused for free.

And in `tests/test_workspace.py` and `tests/test_exclusions.py`, for the
exclusions half of layer 2:

- a vendored-only change over the line cap is refused without exclusions and
  admitted with them;
- an excluded path appears in neither the size count nor the diff;
- a binary file counts as one file and no lines.

Per-run ceilings terminating an over-budget review is layer 3, and lands with
the engine adapter.
