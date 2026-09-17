# Budget governor — design

Status: approved 2026-09-17. Implements ROADMAP item 1, on top of the
[daemon loop](2026-09-17-daemon-loop-design.md) and the merged queue.

## 🎯 Goal

Put the spending rails in place before anything can spend. Today the daemon
fills the queue and nothing drains it; the moment a worker exists, a claim
becomes expensive. This branch makes the claim the point where allowance is
committed, so no review engine can ever be reached outside the governor.

[BUDGET.md](../../BUDGET.md) is the specification. This document records the
decisions that specification left open, the four ambiguities in it that had to
be resolved, and what was deliberately cut.

## 📐 Scope

In:

- `src/pr_review_agent/budget.py` — the governor, the ledger and the windows.
- `queue.py` — an `admit=` hook on `claim()`, and candidate iteration.
- `store.py` — migration 3, the `ledger` table.
- `config.py` — `BudgetConfig`, and reload support.
- `daemon.py` — a `SIGHUP` handler.
- Documentation: `BUDGET.md`, `CONFIG.md`, `STORAGE.md`, `QUEUE.md`, and
  status rows in `ROADMAP.md` / `ARCHITECTURE.md` / `README.md`.

Out, and recorded as known gaps rather than forgotten:

- **The circuit breaker and calibration decay.** Deferred to the engine
  adapter — see [Deferred](#-deferred-and-why) below.
- **The per-contributor cap.** Same.
- **The 60 % `REDUCED` rung.** Same.
- **Layer 2** (path exclusions, diff-size caps, pre-flight estimate). Needs a
  diff in hand, which only the engine adapter has.
- **Layer 3 enforcement** (per-turn abort, max turns, wall-clock timeout).
  Only a running engine can abort a turn. `max_run_tokens` lands here because
  the governor itself reserves against it; `max_turns` and
  `wall_clock_seconds` do not, because nothing would read them.
- **A worker that claims.** Nothing drains the queue in this branch either.
  The `admit` hook ships with tests as its only caller, which keeps
  `ROADMAP.md`'s "the backlog is visible and none of it has cost anything"
  true until an engine exists.

## 🧩 The three windows

Every limit is measured the same way: tokens recorded in the ledger within a
trailing duration. Three windows, one shape.

| Window | Duration | Limit |
| :-- | :-- | :-- |
| `session` | 5 h | `session_tokens × share` |
| `weekly` | 7 d | `weekly_tokens × share` |
| `daily` | 24 h | `weekly_tokens × share ÷ 7` |

`session_tokens` and `weekly_tokens` are the operator's estimate of the
**plan's** limits. `share` is `reviewer_share_pct ÷ 100`, default `0.4`, and
it is applied once when the config is parsed — the share never appears in the
arithmetic again. That multiplication is the human-headroom guarantee from
[BUDGET.md](../../BUDGET.md#-human-headroom): a runaway agent can degrade
interactive Claude Code but cannot lock a maintainer out of it.

The effective ceiling for a run is the **tightest** of the three, and the
ladder rung comes from the **worst** utilisation among them.

### Why daily is a flat seventh

`BUDGET.md` specifies a *rolling* weekly window but computes daily pacing as
`weekly_remaining ÷ days_remaining`. A rolling window never resets, so
`days_remaining` has no value.

Taking it as a constant 7 fails: spend the full daily allowance and tomorrow's
cap is `6W/7 ÷ 7`, which decays geometrically and leaves most of the week
unspent. Anchoring it to a week boundary works — Monday gives `W ÷ 7`,
Tuesday `6W/7 ÷ 6`, the same number — but requires inventing a reset day the
plan does not publish, and getting it wrong is a spending bug.

So daily is `weekly_limit ÷ 7` over a trailing 24 h. No anchor, no coupling to
the weekly window's current state, and a rule that states in one line: **at
most a seventh of the week in any day.**

This is stricter than the anchored version, not looser. An agent idle from
Monday to Thursday cannot burn four days' allowance on Friday. Given that the
point of the exercise is leaving headroom for humans, unspent allowance is not
a loss.

## 🧾 The ledger

One table, migration 3:

```sql
CREATE TABLE IF NOT EXISTS ledger (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key       TEXT NOT NULL,
    owner            TEXT NOT NULL,
    actor_id         INTEGER NOT NULL,
    mode             TEXT NOT NULL,
    reserved_tokens  INTEGER NOT NULL,
    used_tokens      INTEGER,          -- NULL until settled
    usage_confidence TEXT,             -- exact | estimated | unavailable
    engine           TEXT,             -- NULL until settled
    model            TEXT,
    reserved_at      TEXT NOT NULL,    -- aware UTC, ISO-8601
    settled_at       TEXT
);
CREATE INDEX IF NOT EXISTS ledger_window ON ledger (reserved_at);
```

Rows are **never deleted**, including when review content is purged on merge.
The windows are computed from history, so pruning the ledger would silently
hand back allowance that was genuinely spent.

`repo` and `pr_number` are not columns: both dedupe-key namespaces
(`pr_opened:{repo}:{pr}:{head_sha}`, `mention:{repo}:{pr}:{comment_id}`)
already carry them, and `queue` rows are kept forever for the same reason.

`engine` and `model` are NULL at reserve time because no engine exists to name
them; they are filled at settle. `actor_id` is recorded but **not enforced**
against — see [Deferred](#-deferred-and-why).

`state` is not a column: a row is reserved exactly when `settled_at IS NULL`.

## 🔒 Reserve-then-settle

Checking the remaining allowance is not enough under concurrency. Two workers
can each observe sufficient budget, each start a run, and collectively breach
the cap — while both checked correctly.

The reservation is therefore written inside the *same* `BEGIN IMMEDIATE`
transaction as the queue claim. SQLite's single write lock makes the pair
atomic for free, which is
[the main reason the store is SQLite](../../STORAGE.md#-why-sqlite).

One query measures a window, and it serves both settled history and live
holds:

```sql
SELECT COALESCE(SUM(COALESCE(used_tokens, reserved_tokens)), 0)
FROM ledger WHERE reserved_at > :start
```

A settled row counts what it actually spent; an unsettled one counts its full
reservation. That fallback is the entire concurrency guarantee: worker B,
running in the next transaction, sees worker A's reservation as already-spent
even though A has not finished.

### A crashed worker's reservation stays charged

`BUDGET.md` says *"Reservations expire with the lease, so a crashed worker
does not leak allowance."* Read literally, an expired reservation should stop
counting.

It does not, here. A crashed run has probably already spent tokens, and
`queue.DEFAULT_MAX_ATTEMPTS` is 3 — releasing on expiry would let the same
trigger spend invisibly three times over. An unsettled reservation stays
charged until it ages out of its rolling window.

That is not a leak: "leak" means *held forever*, and a rolling window releases
it. It is the difference between not leaking and granting an amnesty, and the
safe direction for a spending control is the pessimistic one.

The consequence is that the ledger needs no `expires_at`, no expiry sweep, and
no state machine — which is most of why this design is smaller than the one
`BUDGET.md` implies.

## 📉 The ladder, and what refusal does to the queue

Three rungs, from the worst utilisation across the three windows:

| Utilisation | Mode | `admit` |
| :-- | :-- | :-- |
| < 85 % | `FULL` | admit |
| ≥ 85 % | `MENTION_ONLY` | refuse `PR_OPENED`, admit `MENTION` |
| ≥ 100 % | `EXHAUSTED` | refuse everything |

`MENTION_ONLY` is what keeps a maintainer in control near the ceiling: auto
review of fresh pull requests stops, conserving the remainder for the pull
request somebody explicitly asks about. `EXHAUSTED` is specified to post a
notice naming the window and its reset time; with no publisher, `admit` logs
it at `WARNING` and the posting lands with the publisher.

A run is also refused, at any rung, when the tightest window's remaining
allowance is below `max_run_tokens`. There is no partial reservation: a run
that cannot be afforded in full is not started.

### Why `LIMIT 1` has to go

`_CLAIMABLE` currently ends in `LIMIT 1`, which is safe today because every
condition in its `WHERE` clause is a fact about the row — attempts, status,
lease, pull request — so SQLite can already exclude anything unclaimable.

Budget refusal is the first decision SQLite cannot express: it depends on the
ledger, the rung and the trigger's kind. It therefore happens in Python, after
`LIMIT 1` has discarded every alternative.

At `MENTION_ONLY` that is a deadlock. A refused `PR_OPENED` row must not
consume an attempt (three budget refusals would mark a legitimate trigger
`abandoned` for a reason unrelated to it) and must not be marked done (it
still deserves review when the window rolls). So it correctly stays `pending`,
correctly stays at the head of a FIFO queue, and blocks the maintainer's
`@claude` behind it until the weekly window resets — days.

`claim()` therefore iterates the ordered candidates and leases the first the
hook admits. When the budget is healthy nothing is refused, the loop stops on
the first row, and the emitted query is what it is today. Only under refusal
does it walk further, over a small pending set, costing no tokens and no
network.

## 🔌 The seam

```python
with self._store.transaction() as conn:
    conn.execute(_ABANDON_EXHAUSTED, {...})
    for row in conn.execute(_CLAIMABLE, common):
        candidate = _claim(row, owner=owner, leased_until=until)
        if admit is None or admit(conn, candidate):
            _take_lease(conn, key=row[0], owner=owner, until=until)
            return candidate
    return None
```

`claim(now=, owner=, admit=None)` keeps its transaction and its SQL; the
governor contributes statements that run inside them. `queue.py` gains no
import from `budget.py`, so the dependency points one way, as
[ARCHITECTURE.md](../../ARCHITECTURE.md#-layering) requires.

The hook is `Callable[[sqlite3.Connection, Claim, datetime], bool]` — a plain
predicate, because a truthy answer is all `claim()` needs and anything richer
would put a budget type in the queue's signature. `Governor.admit` satisfies
it. The `datetime` is the `now` the claim is already working from: the
governor has to measure its windows against the same instant the lease is
computed from, and taking a fresh clock reading inside `admit` would also
make the arithmetic untestable.

`Governor` holds the `SqliteStore`, so `settle()` can open its own
transaction; `admit` is the one method handed a connection from outside.
`settle(claim, usage, *, now)` finds its row by `(dedupe_key, owner)` where
`settled_at IS NULL` — the same pair `queue._FINISH` is guarded on, so a
worker whose lease lapsed cannot settle the row a later worker now holds.

`Usage(tokens, confidence, engine, model)` is one frozen record rather than
four keyword arguments. They are a single answer — what the run cost and how
far that can be trusted — and it is what a posted comment must be traceable
back to.

The reserved row's `mode` is written but **not** read back in this branch: the
engine adapter is what will need to know which rung a run was admitted under,
and it can add the accessor it actually wants rather than inherit a guess.

Rejected alternatives:

- **A required `governor` on `ReviewQueue.__init__`.** Stronger — an
  unguarded claim could not be constructed — but it points `queue.py` at
  `budget.py`, and forces a governor on `daemon.py`, which only enqueues.
- **`claim_within(conn, ...)`, composed by the caller.** No inversion of
  control, but transaction management and refusal-by-rollback move into a
  caller that does not exist yet, and candidate iteration becomes its problem.

The weakness of the chosen shape is that `admit=` is optional, so the
guarantee is a convention rather than a type. It is held by `DESIGN.md`'s one
rule, by review, and by the fact that the only caller that will ever claim is
the worker the engine phase adds.

## ⚙️ Configuration

```yaml
budget:
  enabled: true              # false = a hard stop: nothing is admitted
  session_tokens: 88000      # your estimate of the plan's rolling 5-hour limit
  weekly_tokens: 1500000     # your estimate of the plan's rolling weekly limit
  max_run_tokens: 60000      # reserved per run
  reviewer_share_pct: 40     # optional, default 40
```

The `budget` section is **required**, and so are its three token counts —
including when `enabled` is false. Every one of them is a guess, because the
plan publishes no quota, and a guess that ships as a default is a spending
ceiling nobody chose. That is the failure
[CONFIG.md](../../CONFIG.md#-the-rule-the-loader-follows)'s
reject-unknown-keys rule exists to prevent, and a silent default is the same
mistake wearing a different hat.

Requiring them unconditionally, rather than only when `enabled` is true,
avoids a conditional validator and means flipping the kill switch back on
over `SIGHUP` cannot fail on a key that was never supplied. It makes `budget`
the second required section after `github` and `triggers`, so the shared
fixtures in `tests/test_config.py` gain it.

`reviewer_share_pct` must be between 1 and 100. Zero would make every limit
zero, and utilisation an undefined `0 ÷ 0`; an operator who wants to stop the
agent has `enabled: false`. Each limit is floored to a whole number of tokens
after the share and the seventh are applied.

`bool` is excluded from every numeric key explicitly, because it subclasses
`int`: without that check `weekly_tokens: true` would validate as a
one-token ceiling, silently.

One cross-key check: `max_run_tokens` must fit inside the daily allowance.
The daily window is the tightest of the three, so a run larger than it could
never be admitted at all — an agent that reviews nothing, arrived at by
arithmetic nobody did by hand. It fails at startup instead.

**`enabled: false` stops reviewing; it does not stop checking.** The key name
comes from `BUDGET.md` and cannot be renamed, but the two readings differ by
catastrophe: one is an emergency brake, the other is unbounded spend. `admit`
refuses everything. The docstring and `CONFIG.md` say so in those words.

## 🔁 `SIGHUP`

The daemon re-parses `config.yaml` on `SIGHUP` and swaps the governor's
`BudgetConfig`. It is a frozen dataclass, so the swap is one atomic
assignment, and all SQLite work is on the single loop thread.

Only the `budget` section is hot-swapped. A change to `github`, `store` or
`triggers` is logged as needing a restart rather than half-applied.

**A `SIGHUP` against a broken file must not kill the daemon.** `ConfigError`
is caught, logged at `ERROR`, and the previous config stays in force —
otherwise the emergency brake doubles as a way to take the service down with a
typo.

`publish.dry_run` is the other key `BUDGET.md` requires to be reloadable. It
arrives with the publisher; the mechanism built here takes it without change.

## 🗑 Deferred, and why

Each of these is specified in `BUDGET.md` and is **not** in this branch. They
are listed in `ROADMAP.md` under known gaps so they are not rediscovered as
omissions.

**The circuit breaker and calibration decay.** Every limit above is a guess,
and a guess that is too high is the one failure the rest of the design cannot
see: the governor reports healthy utilisation while the real limit is hit and
a maintainer is locked out. The breaker is the only feedback from reality into
that guess, and the decay is what makes it converge downward instead of
hitting the wall once per window forever.

It is deferred because **its interface is determined by an error nobody has
seen yet.** `trip(window, resets_at)` assumes the failure names which window
blew and when it resets; constraint 4 in
[DESIGN.md](../../DESIGN.md#-the-four-constraints) says the plan exposes no
reset time, so that signature may well be wrong. Designing it against a
synthetic call, then rewriting it when the engine adapter shows what a real
usage-limit error looks like, is worse than designing it once alongside the
detector.

Nothing drains the queue in this branch, so no run can hit a limit and there
is no exposure today. The cost is operator guidance, and `CONFIG.md` carries
it: **until the breaker lands, set `session_tokens` and `weekly_tokens`
conservatively low, because nothing will catch an over-estimate.**

**The per-contributor cap.** The allowlist holds one person. Any cap below
100 % would block the only account that can trigger anything, so it would ship
inert. `actor_id` is recorded on every ledger row regardless — the ledger is
append-only, and attribution is the one field that cannot be backfilled.

**The 60 % `REDUCED` rung.** It means "drop to correctness and security
dimensions only", and there is no engine to thin a review. A rung that records
a mode and changes no behaviour is the same dead-setting problem that keeps
`max_turns` out of the config. It returns with the engine that can honour it.

## 🧪 What the tests must pin

Pure arithmetic over fixtures and a `tmp_path` store. No network, no tokens.

Spend bounds (`CLAUDE.md` §5):

- a synthetic concurrent load over distinct pull requests cannot breach any
  window — the reserve-then-settle guarantee, run with real threads against
  one file-backed database;
- with `reviewer_share_pct: 40`, admitted totals never exceed 40 % of either
  plan window;
- daily pacing refuses a run the weekly window alone would have allowed;
- `enabled: false` admits nothing;
- a run is refused when the tightest window's remainder is below
  `max_run_tokens`;
- an unsettled reservation counts in full against the next claim, and keeps
  counting after its lease would have expired.

Behaviour:

- the ladder switches at exactly 85 % and 100 %;
- at `MENTION_ONLY`, a refused `PR_OPENED` row at the head of the queue does
  **not** block a `MENTION` behind it;
- refusal increments no `attempts` and leaves the row `pending`;
- `settle` releases the unused remainder, and records `engine`, `model` and
  `usage_confidence`;
- a `SIGHUP` against an unparseable file keeps the previous config in force.

## 🔀 Branch boundary

Nothing else is in flight. The daemon branch's boundary table assigned
`budget/`, `store.py` `_MIGRATIONS`, `queue.py` `claim()` and `config.py`'s
`BudgetConfig` to this work, which is what this document implements — except
that `budget/` is a single `budget.py`. At roughly 220 lines it matches
`queue.py` and `daemon.py`; only `triggers/` and `poller/` are packages, and
both hold five or more modules.
