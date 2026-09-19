# Budget layer 2 — path exclusions and the pre-flight estimate

Status: approved 2026-09-18. Implements
[issue #17](https://github.com/prasadtalasila/pr-review-agent/issues/17),
the remainder of [BUDGET.md](../../BUDGET.md)'s second layer.

Layer 2's diff-size caps shipped early, with the
[workspace](../../WORKSPACE.md). What was left is the half that needs a diff
in hand: refusing to *count* paths nobody wants reviewed, and refusing a run
whose predicted cost cannot fit the reservation it would take.

The ordering is the point: **refuse what you can refuse for free, before
paying to find out.**

## 🎯 Goal

Two free refusals, and the bookkeeping that makes the second possible.

1. A vendored-dependency bump is not charged for lines nobody would read,
   and the engine is not shown them either.
2. A pull request predicted to cost more than `max_run_tokens` is refused,
   and the reservation it took is handed straight back.

Like the queue, the governor, the workspace and the engine seam before it,
**all of this lands with no caller.** The worker that sequences facts →
checkout → preflight → engine is the next phase's job.

## 📐 Scope

In:

- `config.py` — one new `budget` key, `excluded_paths`.
- `workspace/exclusions.py` — patterns to git pathspecs. New file.
- `workspace/repo.py` — the size gate moves after the fetch and onto
  `git diff --numstat`; `Checkout` gains the reviewable size.
- `store.py` — migration 5 adds `ledger.reviewed_lines`; `_migrate` becomes
  atomic so a non-idempotent migration is safe.
- `budget.py` — `Governor.estimate`, `Governor.preflight`, and
  `settle(..., reviewed_lines=...)`.
- Documentation: BUDGET.md, WORKSPACE.md, STORAGE.md, CONFIG.md, ENGINE.md,
  ROADMAP.md, `config.example.yaml`.

Out:

- The worker that calls any of it.
- The circuit breaker, layer 3's per-turn enforcement, and the ladder's 60 %
  rung. All three need a *running* engine; this needs only a diff.

## 🚫 Exclusions are git pathspecs

`budget.excluded_paths` is a list of glob patterns. `exclusions.py` turns it
into pathspec arguments:

```text
git diff … -- . ':(exclude,glob)**/package-lock.json' ':(exclude,glob)**/vendor/**'
```

**One mechanism serves both uses the issue requires.** The same argument
list is passed to `--numstat` (which the size gate counts) and to the diff
text (which is what the engine is shown), so the two cannot drift apart. A
future change that excludes a path from one necessarily excludes it from the
other.

That second half holds by construction rather than by care, because
[ENGINE.md](../../ENGINE.md)'s `ReviewRequest` carries the checkout and
deliberately does not repeat the diff beside it: there is exactly one diff in
the system, and it is `Checkout.diff`.

`glob` magic is what makes `**/` mean "at any depth"; without it `*` would
not cross a `/`. `exclude` is applied per pattern rather than to the whole
pathspec, and `.` is listed first because a pathspec of exclusions alone
matches nothing.

### Validation, and why it is narrow

A pattern must be a non-empty string and must not begin with `:`. The second
rule is the load-bearing one: `:(exclude,glob)` is a prefix we build, and a
pattern free to start with `:` could open magic of its own — `:(attr:…)`,
or a bare `:` re-anchoring the path. It cannot reach outside the argument it
sits in, because pathspec magic ends at the `)` we wrote, but a pattern that
rewrites its own semantics is not something an operator can predict from
reading their config file.

Nothing else is validated. These patterns come from `config.yaml` and from
nowhere else — no pull request body, comment or diff contributes one, so
[CLAUDE.md](https://github.com/prasadtalasila/pr-review-agent/blob/main/CLAUDE.md)'s rule that untrusted input must never widen
what the agent may do is satisfied by the data's provenance, not by a filter.

### The defaults

Lockfiles, vendored trees, generated code and minified bundles: the four
categories the issue names, and the ones where reviewing a line is close to
worthless while the line still counts against a cap.

They are defaults rather than a fixed list because a repository that
genuinely reviews its lockfiles exists, and because `budget` is the section
`SIGHUP` reloads — an operator who finds the agent blind to something can fix
it without a restart.

## 📏 The size gate moves after the fetch

It has to. The gate's inputs today are `facts.additions`, `facts.deletions`
and `facts.changed_files`: three aggregates from `GET /pulls/{n}` with no
per-path breakdown at all. There is no way to subtract a lockfile from an
integer.

The alternatives were to read `GET /pulls/{n}/files` — which paginates, needs
up to thirty requests per claimed trigger, truncates at three thousand files,
and is the exact endpoint [WORKSPACE.md](../../WORKSPACE.md) rejected for the
diff — or to keep a second, looser pre-fetch cap that no operator asked for.

So `checkout()` becomes: fetch → `merge-base` → `--numstat` → **gate** →
diff text → `worktree add` → yield.

**What this costs.** "Refused before anything is written to disk" weakens to
"refused before a worktree exists and before any diff can reach an engine".

**Why that is affordable.** The caps are a spending control, and the thing
being spent is tokens. A fetch costs bandwidth and disk; it costs no tokens.
WORKSPACE.md already concedes the point in as many words — the caps "bound
what the engine reads, not the disk" — because a fetch pulls every object
reachable from the head regardless of what the diff reports. The property
being given up was never the one doing the work.

### Counting numstat

One line per changed file, `added`, `deleted`, `path`. A binary file reports
`-` for both counts; it is one file and zero lines. Paths are not parsed at
all — the gate needs two integers and a row count — so a path containing a
newline, which git quotes, cannot confuse it.

`PullRequestFacts.additions` / `deletions` / `changed_files` stay. They are no
longer the gate's input, but they are what the refusal log reports beside the
reviewable figure, which is how an operator understands why a forty-thousand
line pull request was admitted.

`Checkout` gains `reviewed: DiffSize` — files and lines after exclusion. That
is the estimator's input, and the only new thing a caller has to carry.

## 🔮 The pre-flight estimate

`estimate(reviewed_lines) = rate × reviewed_lines`, compared against
`max_run_tokens`.

**No fitted intercept.** A real review has a fixed overhead — the prompt, the
instructions, the first file read — and a two-parameter fit would capture it.
It is not worth it: the intercept is unstable on a handful of samples, and
the only question asked of the estimate is whether a pull request is *large*
enough to refuse. Under-predicting a fifty-line change is harmless, because a
fifty-line change is nowhere near the cap. The fixed cost is amortised into
the rate, where it makes the rate slightly conservative for large diffs —
the safe direction.

### Where the rate comes from, and the column it needed

`rate = Σ used_tokens / Σ reviewed_lines` over settled ledger rows.

The ledger recorded what every run spent but nothing about what it was given,
so there was nothing to fit against. Migration 5 adds `reviewed_lines`.

**It is written by `settle`, from the checkout — not by the engine.**
`ReviewResult.usage` *is* `budget.Usage`, so putting the field there would
have made an adapter responsible for reporting the size it was handed. An
adapter that under-reported would bias the rate downward, which is a spending
control taking its input from the thing it controls. `settle` takes it as a
separate keyword instead, and the worker reads it off `Checkout.reviewed`.

Only rows with `usage_confidence = 'exact'` are fitted. An engine that
reports no usage produces `unavailable` rows, which ENGINE.md says force the
governor onto proxy controls; letting those rows fit a rate of zero would
turn a known-weaker guarantee into a confidently wrong number.

### The cold start

A fresh database has no rows, and the first runs are exactly when an
over-estimate is cheapest to get wrong. The issue asked for this to be
decided and recorded, so:

**`DEFAULT_TOKENS_PER_LINE = 40`, used until `MIN_FIT_SAMPLES = 10` fittable
rows exist.** Then the fitted rate takes over, permanently.

Forty is above what a review is expected to actually cost per changed line,
which is the point — "err high" means over-predicting, which over-refuses
rather than over-spends. With the shipped `max_run_tokens` of 60,000 it puts
the refusal threshold at 1,500 reviewable lines on a fresh install.

The alternative the issue offers — declining to estimate until data exists —
was rejected. It leaves the least-calibrated moment unguarded: a run that
truly costs 200,000 tokens against a 60,000 reservation completes, and the
overrun is discovered at settle, after the tokens are gone. That overrun is
precisely what BUDGET.md says drives a window to `exhausted`.

A permanent floor — `max(fitted, constant)` — was also rejected. After five
hundred runs proving reviews cost twelve tokens a line, it would still refuse
pull requests the agent has direct evidence it can afford. A fit that can
only revise upward is not a fit.

Neither constant is configurable. An operator's escape hatch is
`max_run_tokens`, which they already own and already have to choose; adding a
second knob that interacts with it multiplicatively is
[CONFIG.md](../../CONFIG.md)'s failure mode, not a feature.

## ♻️ Refusal releases the reservation

The issue asks for a refusal "before a reservation is taken". That is not
reachable, and the reason is structural.

`Governor.admit` reserves inside `claim()`'s `BEGIN IMMEDIATE` — the
atomicity BUDGET.md calls the main reason to prefer SQLite. The size facts
come from `GET /pulls/{n}`, which `pulls.py` reads *once per claimed
trigger*, because reading it per open pull request per poll cycle is the
design [POLLER.md](../../POLLER.md) exists to refuse. One of those two would
have to move, and both are load-bearing.

So the criterion is honoured as **"refused before any tokens are spent, and
the reservation is released"**:

```python
Governor.preflight(claim, reviewed_lines, now) -> bool
```

It refuses, and settles the row at zero tokens in the same call, when the
estimate exceeds `max_run_tokens` — or when `reviewed_lines` is zero.

Releasing *inside* the decision rather than beside it is deliberate. A worker
that forgot to release would leave a full reservation charged against every
window until it aged out, and BUDGET.md is explicit that nothing releases a
reservation early. Making the refusal and the release one call means that
failure cannot be introduced by a caller.

The zero-line case is beyond the issue's checklist. A lockfile-only pull
request now has nothing left to review after exclusion, and running an engine
over an empty diff is the cheapest refusal available.

A refused run settles `used_tokens = 0` with `usage_confidence = 'exact'` and
no engine or model — the cost is not unknown, it is known to be nothing. Its
`reviewed_lines` stays `NULL`, so a refusal never contributes to the fit.

## ✅ Verification

The full local gate — `pytest`, `ruff`, `pylint`, `pyright` — plus, pinning
the issue's acceptance list specifically:

- a pattern list becomes the expected pathspec arguments, and an empty list
  produces none;
- `excluded_paths` rejects an empty entry and one beginning with `:`;
- **a vendored-only change is not refused on size**, over a fixture repository
  whose vendored file alone exceeds `max_changed_lines`;
- an excluded path appears in neither `Checkout.diff` nor `Checkout.reviewed`;
- a binary file counts as one file and zero lines;
- an empty ledger estimates at the documented constant, and refuses an
  oversized pull request;
- the fitted rate takes over at `MIN_FIT_SAMPLES`, and ignores rows whose
  confidence is not `exact`;
- `preflight` refusing leaves the window measuring zero — the reservation is
  genuinely back;
- a zero-reviewable-line pull request is refused;
- migration 5 upgrades a v4 database and leaves existing rows `NULL`.

All of it runs over fixtures. No network, and no tokens.
