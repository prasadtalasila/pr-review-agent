# Budget layer 3: the per-run wall clock, and what a subprocess cannot enforce

Design for issue
[#19](https://github.com/prasadtalasila/pr-review-agent/issues/19).

Issue #19 was written against the Claude Agent SDK, as
[#18](https://github.com/prasadtalasila/pr-review-agent/issues/18) was. That
premise is gone:
[#24](https://github.com/prasadtalasila/pr-review-agent/pull/24) settled that
every engine adapter is a command-line tool run as a subprocess and that no
vendor SDK is linked. Two of #19's three ceilings do not survive the change
intact, and a third has already been built by somebody else. This document is
what layer 3 actually is once the subprocess boundary and the merged
[review worker](../../WORKER.md) are taken into account.

The change is small. Most of the work here was establishing that it *should*
be small, which is why the reasoning is longer than the diff.

## What #19 asked for, and where each piece went

| #19's ceiling | Under the SDK | After #26 and #29 |
| :-- | :-- | :-- |
| `max_turns` | native in `ClaudeAgentOptions` | the worker's, and already built |
| `wall_clock_seconds` | an `asyncio` timeout around the run | built as `engine.timeout_seconds`; **its validation is missing** |
| per-turn token abort | SDK lifecycle hooks | no equivalent; deliberately not built |

Only the middle row is unfinished, and only partly. The other two need
arguing rather than implementing.

### `max_turns` belongs to the worker, and the worker has it

A turn cap bounds how often the agent may be asked to think about one pull
request. Under the SDK that was a single option on a single call. Under a
subprocess it splits in two, and the halves land in different places.

*Inside* one `review()` call, turns are the CLI's business. `claude 2.1.274`
does accept a `--max-turns` flag, but it is absent from `--help` — the
binary rejects an unknown option outright, so passing it proves it exists,
and it being undocumented means it can be withdrawn without a deprecation.
More to the point, the CLI already ends such a run with `subtype:
error_max_turns`, which
[`ClaudeCliEngine._outcome`](https://github.com/prasadtalasila/pr-review-agent/blob/main/src/pr_review_agent/engine/claude.py)
maps to `Outcome.TRUNCATED`. The behaviour #19 wanted is present; only the
ability to *choose* the number is missing, and it is missing behind an
undocumented flag.

*Across* calls, turns are ours, and `queue.py` already bounds them:

```python
# Three runs is enough to ride out a transient failure and few enough that a
# poison trigger cannot drain the weekly allowance one retry at a time.
DEFAULT_MAX_ATTEMPTS = 3
```

`ReviewWorker._finish_for` sends `TRUNCATED` to `queue.release` for another
attempt and `FAILED` to `queue.abandon`, and every attempt reserves
`max_run_tokens` again through `governor.admit`. So the number of times one
trigger can reach an engine is already capped, by us, in code that spends
nothing to enforce it.

Adding `budget.max_turns` would therefore add a second bound on a quantity
that is already bounded, readable only through a flag that may not exist next
release. **`max_turns` is struck from layer 3.** `BUDGET.md`'s layer table
and `CONFIG.md`'s standing promise of the key are corrected to say where the
cap actually lives.

### The per-turn token abort is not built, and the gap is stated rather than papered over

#19's sharpest sentence is that the governor's reservation is currently a
forecast rather than a ceiling: nothing stops a run spending more than it
reserved, and `BUDGET.md` records that the `exhausted` rung is reachable
*only* by such an overrun. That is true, and this change does not fix it.

Three ways to fix it were considered.

**A dollar ceiling.** `--max-budget-usd` is real and documented. It is also
denominated in dollars while every window in this system is denominated in
tokens, so using it means either a second ceiling unrelated to the
reservation, or a configured price per million tokens — a number that goes
stale silently and is wrong the moment `--fallback-model` fires. Rejected.

**A wall clock derived from the reservation.** Kill the run at
`max_run_tokens / tokens_per_second`, with the rate fitted from the ledger
the way `Governor._rate` already fits tokens per line. Rejected on the
relationship rather than the machinery: a review spends most of its wall
clock in tool round-trips that cost almost nothing. A run that greps twenty
files burns a minute and a handful of tokens; a run that writes one long
analysis burns ten seconds and thousands. Seconds and tokens are not
monotonically related even within one model, and across `--effort` levels and
cache hit rates the spread is worse. A fitted rate would be a fabricated
conversion dressed as a measurement, which is what
`DEFAULT_TOKENS_PER_LINE`'s own comment argues against.

**Watching tokens in flight.** `--output-format stream-json` emits one JSON
object per message, and assistant messages carry `message.usage`. An adapter
could accumulate that and kill the subprocess when the cumulative count
passed the reservation. This is the honest version of #19's third ceiling —
the stream is the lifecycle hook, arriving on a pipe instead of a callback —
and it is the one that would let a killed run settle at something close to
what it actually spent.

It was rejected too, for a reason that is about the seam rather than about
`claude`. **It assumes the engine reports tokens.** `Capabilities` carries
`usage_reporting` precisely because some engine will not, and
[ENGINE.md](../../ENGINE.md) already writes down what happens then: such an
engine "forces the governor onto proxy controls only — run count, wall clock,
turn caps." A layer 3 whose only ceiling were token-based would contradict
that sentence outright: a `codex` or `opencode` adapter declaring
`usage_reporting: false` would have *no* per-run ceiling at all. Building the
strong control first and the universal one never is the wrong order.

So layer 3 ships the control every adapter can honour, and says plainly that
the reservation is not yet a ceiling. The overrun it leaves is the problem
[#20](https://github.com/prasadtalasila/pr-review-agent/issues/20) exists to
solve — a breaker that converges on the real limit is the right shape for a
bound nobody can enforce mid-run — and a token watchdog gated on a new
capability remains available to it. Deferring is recorded here so #20 does
not have to rediscover the argument.

## What this change builds

### The wall clock is validated against the lease

`engine.timeout_seconds` exists, defaults to 900 seconds, and is enforced by
`asyncio.wait_for` in `CliEngine.run`. Nothing checks how large it is. That
matters because `queue.py` already asserts a relationship it does not
enforce:

```python
# Comfortably above the per-run wall-clock ceiling the budget governor
# enforces, so a live worker never loses its lease; see the module docstring.
DEFAULT_LEASE = timedelta(minutes=30)
```

The module docstring goes further: leases carry an expiry rather than a
heartbeat *because* a run has a wall-clock ceiling, so "a lease renewal would
be machinery for a case that cannot arise." An operator who sets
`timeout_seconds: 3600` makes that case arise. The lease lapses under a live
worker, a second worker claims the same pull request and reserves against the
same windows, and the first worker's `settle` returns `False` and discards a
review that was paid for. Nothing warns; the invariant is a comment.

**`EngineConfig.parse` rejects a timeout at or above `queue.DEFAULT_LEASE`.**
A `ConfigError`, so the daemon refuses to start rather than running with a
lease that can expire under it — the same treatment `BudgetConfig.parse`
already gives `max_run_tokens` exceeding the daily allowance. This is #19's
second acceptance criterion, and it is the whole of the enforcement work.

Strictly below, not equal: at exactly the lease the two expire together and
which one wins is a scheduling race.

`DEFAULT_LEASE` is imported from `queue` into `config`. `queue` imports
`store` and `triggers.models` and not `config`, so there is no cycle.

Nothing else bounds the number. A fixed maximum below the lease was
considered and dropped as a second ceiling needing its own justification,
when `max_run_tokens` is already the operator's escape hatch.

### The ledger records why a run stopped

#19's fourth criterion is that an aborted run be distinguishable in the
ledger from a clean one. Today it partly is — a timeout settles `unavailable`
where a clean run settles `exact` — but `unavailable` covers every post-engine
failure alike, so an operator reading the ledger cannot tell a run killed on
the clock from a crashed CLI from an unreadable envelope.

A sixth migration adds `stop_reason TEXT` to `ledger`, and `Governor.settle`
takes it alongside `reviewed_lines`. `StopReason` is a `StrEnum` in
`budget.py`:

| Value | Written when |
| :-- | :-- |
| `completed` | `Outcome.COMPLETED` |
| `truncated` | `Outcome.TRUNCATED` |
| `failed` | `Outcome.FAILED` |
| `timeout` | the engine outlived `engine.timeout_seconds` and was killed |
| `engine_error` | any other failure of the engine itself |
| `refused` | the pre-flight estimate refused the run, before any spend |
| `infrastructure` | a GitHub or workspace failure, before the engine started |

Seven values rather than a free-text string: the column is read by an
operator and, later, by #20's breaker, and a bounded set is what makes
`GROUP BY stop_reason` mean anything.

`timeout` is separable from `engine_error` only because `CliEngine` raises a
distinct `EngineTimeout`. `ReviewWorker._review` currently flattens every
engine failure into `EngineError`, which is the right boundary for *retry*
decisions and the wrong one for this: the reason is preserved on the
`EngineError` rather than recovered by inspecting `__cause__`.

**Settlement amounts and confidences do not change.** A timed-out run still
settles at its full reservation with `usage_confidence: unavailable`, exactly
as #29 merged and as ENGINE.md describes. The measurement genuinely is
absent — the process was killed before it printed anything — and `unavailable`
is the value that says so. `stop_reason` carries the distinguishing
information, which is all the criterion asks for, and it carries it without
stretching `UsageConfidence` into meaning something it does not.

This also leaves `_FIT_SAMPLE` untouched: it selects on
`usage_confidence = 'exact'`, so killed runs stay out of the pre-flight rate
fit, where a run that never finished is not a sample of what a finished
review costs.

## What #19's acceptance criteria become

| Criterion | Disposition |
| :-- | :-- |
| `max_turns` and `wall_clock_seconds` accepted, validated and enforced | Amended. `wall_clock_seconds` is `engine.timeout_seconds`, now validated; `max_turns` is the worker's `max_attempts`. |
| `wall_clock_seconds` validated below the queue lease, startup error if not | **Met.** |
| A run exceeding its reservation is aborted and settles at what it spent | **Not met, deliberately.** Argued above; deferred to #20. |
| An aborted run yields no publishable findings, distinguishable in the ledger | **Met.** Findings were never at risk — a killed run produces no `ReviewResult` at all — and `stop_reason` supplies the distinction. |
| A test drives a stub engine past each of the three ceilings | **Partly met.** One ceiling exists to be driven past, and a test drives a stub engine past it. |

The issue is edited to match before the work is claimed as closing it. A
checklist quietly left unticked is how a gap becomes folklore.

## Testing

The trigger suite is pure functions over fixtures; none of this needs a
network or spends a token.

- `engine.timeout_seconds` at, above and below `DEFAULT_LEASE` — the first two
  raise `ConfigError` naming both numbers, the third loads.
- The bound is pinned against `queue.DEFAULT_LEASE` itself rather than against
  a literal `1800`, so changing the lease cannot leave the check behind.
- A stub engine that hangs is killed at its configured clock, and the ledger
  row for that run reads `stop_reason: timeout`, `usage_confidence:
  unavailable`, `used_tokens` equal to the reservation.
- Each `StopReason` is reached through the worker by the path that should
  produce it, so the mapping is pinned by behaviour rather than by a
  table-driven test of the enum.
- A settled row still counts in the windows exactly as before, proving the
  new column changed no arithmetic.
- The migration applies to a database created at `user_version = 5` and to a
  fresh one.

## Out of scope

- **A token-denominated run ceiling.** Argued above; #20's.
- **`engine.effort`.** `--effort` is one of the largest multipliers on what a
  run costs and is not in the argv today, so it is a real gap — but it is a
  spending *lever*, not a ceiling, and adding it here would widen a
  ceilings change into engine configuration.
- **Making `max_attempts` configurable.** It is the turn cap now, but nothing
  in #19 asked for it to be tunable and no operator has wanted it.
- **Anything that widens what triggers a review.** Nothing here does; the set
  of things that can spend is unchanged.
