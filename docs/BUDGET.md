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
| 3 | Per-run ceiling: max tokens, turn cap, wall-clock timeout | **done**, with one gap — see below |
| 4 | Rolling windows and pacing, by reserve-then-settle | **done** |
| 5 | A degradation ladder rather than a hard stop | **done** |
| 6 | A circuit breaker, converging onto the limit nobody publishes | **done** — [below](#-the-circuit-breaker) |

Three more bound spend without being layers, because they are properties of
how the agent runs rather than decisions about one claim. They are named here
so that "every control on spend" is one list rather than four pages:

| Control | What it bounds | Where |
| :-- | :-- | :-- |
| `worker.count` | How many reviews can be in flight at once, and so how fast the windows can be drawn down | [WORKER.md](WORKER.md#-workercount) |
| `engine.timeout_seconds` | The wall clock one run cannot outlive, which is the only bound when an engine reports no usage | [ENGINE.md](ENGINE.md) |
| `queue.max_attempts` | How often one trigger may be retried, and so how many times a single failure can be paid for | [QUEUE.md](QUEUE.md) |

Layer 1 is the classifier — it *is* the first budget layer, which is why its
rejections are logged at a level an operator actually sees.

### Where layer 3's three ceilings ended up

This layer was specified against an SDK, and the move to
[a subprocess seam](ENGINE.md) scattered it. None of the three is a
`budget` key, and one of them is not enforced at all.

**Max tokens** is `max_run_tokens`, reserved up front by `admit`. Done.

**The turn cap** is ours, not a flag. Inside one run the CLI ends an
over-long conversation itself, with `error_max_turns`, which the adapter maps
to `TRUNCATED`. Across runs, `queue.DEFAULT_MAX_ATTEMPTS` bounds how often a
single trigger can reach an engine at all, and each attempt reserves again
through `admit`. A `budget.max_turns` key would be a second bound on an
already-bounded quantity.

**The wall clock** is `engine.timeout_seconds`, enforced by the subprocess
boundary and — since layer 3 — validated at startup as strictly below
`queue.DEFAULT_LEASE`. That relationship used to be a comment. It matters
because the lease carries an expiry rather than a heartbeat *precisely*
because a run cannot outlive its clock: break it and the lease lapses under a
live worker, a second worker reserves against the same windows, and the first
one's `settle` discards a review that was paid for.

**The gap: a reservation is still a forecast, not a ceiling.** Nothing stops a
run spending more than it reserved, which is why
[the `exhausted` rung](#-the-degradation-ladder) is reachable only by an
overrun. Every way to close it assumes the engine reports tokens —
`--max-budget-usd` is denominated in dollars, a wall-clock-to-token conversion
is a fabricated rate, and watching usage stream by needs an engine that
streams usage — while [`Capabilities.usage_reporting`](ENGINE.md#-capabilities)
exists because some engine will not report any. Building the strong control
first and the universal one never is the wrong order, so the clock ships and
the token ceiling goes to
[#20](https://github.com/prasadtalasila/pr-review-agent/issues/20), whose
breaker is the right shape for a bound nobody can enforce mid-run. The
argument is recorded in
[the layer 3 design note](superpowers/specs/2026-09-18-budget-layer-3-design.md).

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

The [worker](WORKER.md#the-pre-flight-estimate) calls it once, after the
checkout and immediately before the engine: the tree has to be on disk for
the reviewable line count to exist, and nothing may be spent after it says
no. A refused row is abandoned rather than retried, since the same head
predicts the same cost.

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

Only a **completed** review writes it. A truncated run was cut off with work
outstanding, a failed one produced nothing, and a usage-limited one settles
at `exact` zero — each spent less than reviewing those lines actually costs,
so each would fit a rate below the truth and the estimate would refuse less
than it should. Every other run leaves the column NULL, which is how a row
says nothing about tokens per line rather than saying something wrong.

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

```text
time ──►

worker 1:  BEGIN IMMEDIATE ── measure window ── reserve + claim ── COMMIT ── run ── settle
worker 2:                            BEGIN IMMEDIATE ── measure window ── refused: window full
                                      (worker 1's reservation already counts as spent,
                                       before worker 1 has finished running)
```

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

## 🔌 The circuit breaker

**Implemented** in `src/pr_review_agent/budget.py`, over the `budget_state`
table. Design note:
[the circuit breaker](superpowers/specs/2026-09-18-circuit-breaker-design.md).

Every limit above is the operator's guess, because the plan publishes no
quota. A guess that is too low is harmless. A guess that is too high is the
one failure the rest of this design cannot see: the governor admits runs and
reports healthy utilisation while the real limit is already being hit, and the
[worker](WORKER.md) retries into the same wall until `max_attempts` runs out,
spending each time. The breaker is the only feedback from reality into that
guess.

**Trip.** A usage-limit failure refuses every claim for `TRIP_HOLD`, which is
one `SESSION`. Five hours because it is the shortest window, and because it is
a *duration* rather than a reset time — the only kind of answer available when
[constraint 4](DESIGN.md#-the-four-constraints) says the plan exposes none.
`trip(window, resets_at)`, the signature this document used to sketch, is
therefore not what was built: `trip(now)` takes no reset time because there is
none to take.

If the weekly limit was the one that blew, the next attempt trips again and the
calibration keeps shrinking — the design converges either way, so it never
needs the attribution to be right. A refused claim costs no attempt:
`ReviewQueue.claim` already skips a candidate `admit` rejects rather than
burning one, because a refusal is about the allowance, not about the trigger.

**Decay, and recovery.** The same trip multiplies a stored calibration by
`DECAY_FACTOR` (0.9), and every window's effective limit becomes `configured ×
calibrated`. So the ceiling drops a tenth each time reality disagrees with the
guess, converging downward over a handful of windows instead of hitting the
wall once per window forever.

Recovery is additive: one percentage point per clean `SESSION`, capped at the
configured value. Multiplicative down and additive up is what makes the series
converge rather than oscillate. It exists because the pool is *shared* — a trip
does not always mean the guess was too high, it can equally mean the maintainer
had a heavy week, and a calibration that could only ever be revised downward
would leave the reviewer permanently crippled by one of those. That is the
mirror of the permanent floor [the fit rejects](#the-cold-start-errs-high).

Because a trip holds for a full `SESSION` and recovery accrues only in clean
ones, decay is self-rate-limiting: no second timer, and no way for a burst of
trips to collapse the calibration in an afternoon.

The calibration is an **integer percentage floored at 1**, never a float.
`_headroom` divides by the limit, so a calibration reaching zero would raise
where it should refuse — the same reason `reviewer_share_pct` is validated
`1..100`. Decay truncates rather than rounds, because `round(3.6)` is 4 and a
rounded decay stalls at 4 % forever instead of converging.

**What a tripped run settles at.** Not the full reservation. A usage limit is
one of the few failures where the spend *is* knowable: refused up front the CLI
did no work and it is zero, hit mid-run the envelope measured it. Charging the
ceiling would write tokens that were never spent into all three rolling
windows, and — the windows being rolling and ledger rows never deleted — they
would keep refusing real runs for up to a week after the account recovered.

### Detection is a guess, and this is where to correct it

Issue #20 required the real usage-limit failure to be observed before the
interface was fixed. **It has not been.** Manufacturing one means driving a
live subscription into the wall this design exists to avoid.

So the guess is confined to one constant, `_USAGE_LIMIT_MARKERS` in
`src/pr_review_agent/engine/claude.py`, matched against both plausible
carriers — a nonzero exit's stderr and the result envelope. When the real
error is seen, editing that tuple is the whole fix: no signature changes and
no migration. A miss costs a retry; a false positive takes the reviewer
offline for five hours, which is why the markers are narrow.

## 🕳 Not built yet

Recorded here so they are not rediscovered as omissions.

**Nothing aborts a run that is exceeding its reservation.** `max_run_tokens`
is reserved against and settled against, but a run that overruns it does so
undetected until `settle`, after the tokens are gone — which is why the
`exhausted` rung is reachable only by an overrun. Layer 3
[considered and rejected](#where-layer-3s-three-ceilings-ended-up) every way
to enforce it at a subprocess seam.

The breaker above is **not** that enforcement, and should not be read as it:
it answers the *account's* limit being reached, not one run outspending its
own reservation. What absorbs an overrun today is the
[pre-flight estimate](#-the-pre-flight-token-estimate), whose rate is fitted
against what runs actually cost — so an expensive run raises the predicted
cost of the next comparable one, and a large enough pull request is refused
before it starts. That is feedback after the fact rather than a ceiling, and
the difference is a reservation's worth of tokens.

**The ladder's 60 % rung** likewise waits on a running engine to degrade.

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
  pull request with nothing left to review is refused for free;
- a trip refuses every claim without costing an attempt, admission resumes
  once the hold expires, and the calibration decays, bounds every window,
  floors above zero and survives a restart;
- repeated trips drive the calibration **monotonically downward** rather than
  oscillating, and a clean window recovers a point, capped at the configured
  value.

In `tests/test_cli_engine.py` and `tests/test_worker.py`, for the detector and
the join:

- a usage limit on stderr and one in the envelope both raise `UsageLimited`,
  the envelope carrying its measured usage and the stderr case carrying none,
  while an unrelated failure is still a protocol error;
- a `UsageLimited` run trips the breaker, settles at what is known rather than
  at the reservation, leaves the row pending, and is not attempted again while
  the breaker holds.

And in `tests/test_workspace.py` and `tests/test_exclusions.py`, for the
exclusions half of layer 2:

- a vendored-only change over the line cap is refused without exclusions and
  admitted with them;
- an excluded path appears in neither the size count nor the diff;
- a binary file counts as one file and no lines.

And in `tests/test_config.py`, `tests/test_store.py` and
`tests/test_worker.py`, for layer 3:

- a review wall clock at or above the queue lease fails startup, pinned
  against `DEFAULT_LEASE` itself rather than against `1800`, so changing the
  lease cannot leave the check behind;
- a run killed on the wall clock is distinguishable in the ledger from one
  whose engine merely fell over, and both still settle at the full
  reservation with `unavailable` confidence;
- a pre-flight refusal reads as `refused` rather than as a failure.

Terminating a review that is *over budget*, as opposed to over time, is the
[gap named above](#where-layer-3s-three-ceilings-ended-up). It is not tested
here because it is not built.
