# Configuration reference

One file, `config.yaml`, next to the daemon. `config.yaml` is gitignored: it
names real accounts and will later sit beside the agent's credentials.

Two templates ship *inside the installed package*, and `config generate`
writes one out. Both are parsed by the test suite, so neither can drift out
of step with the loader:

| Template | What it is | Written by |
| :-- | :-- | :-- |
| [`config.minimal.example.yaml`](https://github.com/prasadtalasila/pr-review-agent/blob/main/config.minimal.example.yaml) | The smallest file that loads — every required key and nothing else. Start here. | `config generate` |
| [`config.example.yaml`](https://github.com/prasadtalasila/pr-review-agent/blob/main/config.example.yaml) | Every key the loader accepts, with the reasoning behind each. Values shown for optional keys are the defaults. | `config generate --full` |

```bash
pr-review-agent config generate          # writes ./config.yaml
pr-review-agent config validate          # loads it and reports what it found
```

`config generate` refuses to overwrite an existing file unless `--force` is
given: a `config.yaml` names real accounts, is gitignored, and has no copy
anywhere. `config validate` needs no `GITHUB_TOKEN` — it answers a question
about a file.

The two copies at the repository root are the same bytes, kept for anyone
working from a clone; a test asserts they match the packaged ones.

## 🧾 The rule the loader follows

**Unknown keys are rejected, not ignored.** A typo in a safety setting must
fail at startup, not silently fall back to a default that spends tokens. That
applies to unknown top-level sections and to unknown keys inside a section.

Only the sections backed by implemented components are accepted today. The
`publish` section arrived with the component that reads it rather than
before it — accepting settings that do nothing is the failure this rule
exists to prevent. The same rule is why `budget` carries `max_run_tokens`
but no turn cap: the governor reserves against the first, while the second
is not a setting at all — `queue.DEFAULT_MAX_ATTEMPTS` bounds how often one
trigger may reach an engine, and the CLI bounds the turns inside a single
run.

## 🔐 What is *not* in this file

Credentials. The GitHub token is read from the `GITHUB_TOKEN` environment
variable, so it can come from a systemd `EnvironmentFile`, a secret manager or
the shell without ever being a file the repository could swallow. `config.yaml`
is gitignored regardless, because it names real accounts.

## 📖 Sections

### `github`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `repo` | `owner/name` | yes | The repository to poll. Reviews are posted here. |
| `agent_user_id` | integer | yes | The numeric id of the account the agent posts as. Its own comments are then ignored, so a posted review can never re-trigger a review. |

`repo` must contain exactly one `/`, with both halves non-empty.

`agent_user_id` has no default and the loader refuses a file that omits it.
Without it the `self_author` / `self_commenter` rejections never fire, so the
agent can answer its own review — a loop that spends real tokens and is only
visible after it has run. Like the allowlist it is a **numeric id, never a
login**: a login can be renamed and the freed name registered by a stranger.

### `triggers`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `allowlist` | list of integers | yes | Numeric GitHub user ids whose events may start a review. |
| `handle` | string | no (default `claude`) | The handle that summons a review. A leading `@` is stripped. |

**`allowlist` entries must be numeric user ids, never logins.** A login can be
renamed and the freed name registered by somebody else, which would hand
eligibility to a stranger. A login in this list raises `AllowlistConfigError`
at startup rather than never matching — so does a non-positive number, and so
does a string like `"--5"` that only looks numeric.

Look an id up with:

```bash
curl -s https://api.github.com/users/<login> | grep '"id"'
```

An empty allowlist is valid and allows nobody. It is the safe starting state.

`handle` is what makes the pipeline reusable for a different agent: set it to
`aider` and `@aider` becomes the trigger.

### `budget`

Required, and the only section `SIGHUP` reloads. The full specification is
[BUDGET.md](BUDGET.md); this is the key list.

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `session_tokens` | integer | yes | Your estimate of the plan's rolling 5-hour allowance. |
| `weekly_tokens` | integer | yes | Your estimate of the plan's rolling 7-day allowance. |
| `max_run_tokens` | integer | yes | Reserved up front for one review, released down to actual usage when it settles. |
| `enabled` | boolean | no (default `true`) | `false` **stops reviewing**. It does not turn the budget checks off. |
| `reviewer_share_pct` | integer 1–100 | no (default `40`) | The share of each plan window the agent may use, never the whole allowance. |
| `per_contributor_pct` | integer 1–100 | no (default: **no cap**) | The share of the agent's weekly allowance any one contributor may spend, over the same rolling week. |
| `max_changed_files` | integer | no (default `100`) | A pull request touching more *reviewable* files is refused before a worktree exists. |
| `max_changed_lines` | integer | no (default `5000`) | The same, for additions plus deletions. |
| `excluded_paths` | list of glob patterns | no (defaults below) | Paths counted against neither cap and not shown to the reviewer. |

**The three token counts have no defaults, deliberately.** A subscription
publishes no quota, so every one of them is a guess the operator has to
make, and a guess that shipped as a default would be a spending ceiling
nobody chose. They are required even when `enabled` is `false`, so that
flipping the kill switch back on over `SIGHUP` cannot fail on a key that was
never supplied.

Set them **conservatively low** anyway. The
[circuit breaker](BUDGET.md#-the-circuit-breaker) now catches an over-estimate
and decays the effective limits toward the real one, but it only learns by
hitting the wall: every trip is a lockout the operator could have avoided by
guessing lower to begin with.

`max_run_tokens` must fit inside the daily allowance — a seventh of the
weekly limit, after `reviewer_share_pct` — or the file is refused. A run
that cannot fit in the tightest window could never be admitted at all, which
is a configuration that reviews nothing, arrived at by arithmetic nobody did
by hand.

**`per_contributor_pct` is meaningless on a one-person allowlist.** The one
account able to trigger anything would simply meet its own cap, so leaving it
unset — the default — is right until several people can trigger reviews and
one of them monopolising the week becomes a real outcome rather than a
hypothetical. When it is set, a contributor past 85 % of their own share
stops being auto-reviewed but can still summon a review with `@claude`, and
is refused entirely at 100 %; everyone else's headroom is measured
separately. The same fit rule as the daily window applies: if the resulting
allowance is below `max_run_tokens` the file is refused.

The two diff-size caps are layer 2 of the budget, enforced by the
[workspace](WORKSPACE.md) but configured here so that every spending cap
lives in one section. Unlike the token counts they *do* have defaults: a
plan's allowance is unpublished, whereas a diff-size cap is an ordinary
engineering choice. `tests/test_config.py` pins both by value, so widening
one is a visible diff.

Be clear about what they bound: **the reviewer's input, not the disk.** A
fetch pulls every object reachable from the head, so a commit that adds a
large blob and a later one that removes it still downloads it while
reporting no changed lines.

**`excluded_paths` is subtracted from both caps *and* from the diff the
reviewer is shown** — one list, one mechanism, so the two cannot disagree.
Without it a vendored-dependency bump is refused on a size cap for thousands
of lines nobody would have read. The default covers lockfiles, `vendor/`,
`node_modules/`, `third_party/`, generated code (`*.pb.go`, `*_pb2.py`,
`*.generated.*`) and minified output (`*.min.js`, `*.min.css`, `*.map`); the
full list is in `config.example.yaml`.

Setting the key **replaces** that list rather than extending it, and `[]`
excludes nothing. Patterns are globs matched at any depth via `**/`, and one
may not begin with `:` — the pathspec magic is the agent's to supply.

Because the caps are measured after exclusion, they are checked once the diff
exists rather than before the fetch: three aggregate integers from the API
have no per-path breakdown to subtract a lockfile from. See
[BUDGET.md](BUDGET.md#the-size-gate-moved-to-make-this-possible).

`max_turns` and `wall_clock_seconds` were once promised here and are **not**
coming. Layer 3's wall clock is `engine.timeout_seconds`, where the code that
enforces it lives; its turn cap is not a setting at all. See
[BUDGET.md](BUDGET.md#where-layer-3s-three-ceilings-ended-up).

### `store`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `path` | string | no (default `state.db`) | The SQLite file holding watermarks, ETags and the review queue. |

The whole section is optional, which is one of the two exceptions to "only
the sections backed by implemented components are accepted" being paired with
a required section. The exception is affordable because the default cannot
spend anything: a database that does not exist yet has its watermarks
[seeded to the moment the daemon started](DAEMON.md#-cold-start-is-the-spend-bound),
so a fresh file reviews nothing from the backlog. Unknown keys inside `store`
are still rejected.

A relative path is resolved against the working directory the daemon is
started in, so the daemon logs the absolute path it settled on at `INFO`.
Prefer an absolute path under a service account's data directory in
production — pointing at the wrong file costs the queue's memory of what has
already been reviewed.

### `workspace`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `cache_dir` | string | no (default `.cache/repos`) | Where the bare mirror and the per-run checkouts live. Created `0700`. A relative path is resolved against the daemon's working directory **once, at startup**, and the absolute result is logged; it is not re-read on `SIGHUP`. |

The **second** optional section, and the argument differs from `store`'s. It
is affordable because the section holds a path and nothing else: the setting
that bounds what a checkout may cost lives in `budget`, with every other
spending cap. Unknown keys inside `workspace` are still rejected.

A relative path is resolved against the working directory the daemon starts
in, and logged absolute at `INFO`, exactly as `store.path` is.

### `worker`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `count` | integer 1–4 | no (default `1`) | How many reviews may run at once. |

The **third** optional section, and the only one whose value is a spending
control. Every concurrent run reserves `budget.max_run_tokens` up front, so
`count` multiplies the floor below which the governor refuses everything —
which is why it is capped, and why `tests/test_config.py` pins both the
default and the cap by value.

Raising it parallelises *across* pull requests only. One pull request is
never reviewed by two workers whatever this is: the
[per-pull-request lease](QUEUE.md#-one-pull-request-one-worker) holds that.

It needs a restart to take effect — `SIGHUP` swaps only `budget`, and the
workers are built once at startup. See [WORKER.md](WORKER.md#-workercount).

### `engine`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `model` | string | yes | Passed to the CLI's `--model`. |
| `expected_version` | string | yes | What the adapter was written against. A mismatch warns; it does not refuse. |
| `timeout_seconds` | number | yes | Wall clock for one review. The process is killed past it. Must be **below the 30-minute queue lease**, or the daemon refuses to start. |
| `binary` | string | no (default `claude`) | The executable to run, found on `PATH`. |
| `standards_paths` | list of strings | no (default none) | Files in the *reviewed* repository holding its review standards. |

**Required.** It was optional only while nothing drained the queue, because
an engine nothing calls cannot spend; the [worker](WORKER.md) calls it now, so
a daemon configured without this section would claim work, reserve allowance
and then have nothing to run. This is the section that makes the agent able
to spend real money.

Inside the section nothing is softened. `model` and `expected_version` have
no defaults for the same reason the plan token counts have none — a default
model is a cost nobody chose, and a default version pin is a claim about
output nobody checked. `timeout_seconds` is a spending setting and not merely
a liveness one: a killed run leaves tokens spent with no envelope to measure
them.

**`timeout_seconds` is bounded above by the queue lease**, and a value at or
above it is a startup error rather than a warning. The lease carries an
expiry rather than a heartbeat *because* a run cannot outlive its clock; an
hour-long timeout breaks that, and the failure is silent — the lease lapses
under a live worker, a second worker claims the same pull request and
reserves against the same windows, and the first worker's settlement is
rejected, discarding a review that was already paid for. Strictly below, not
equal: at exactly the lease the two expire together and which one wins is a
scheduling race. See [QUEUE.md](QUEUE.md#-one-pull-request-one-worker).

**`standards_paths` are read at the merge base, never at the pull request
head.** The engine is generic and the standards are per-repository, so they
have to come from the repository under review — but a pull request that could
rewrite the reviewer's instructions has talked its way past every other
control in the system. Reading at the merge base narrows the trusted set to
people who can merge to the base branch. That set is smaller, not empty, and
[DESIGN.md](DESIGN.md#-prompt-injection-is-in-scope) says so. A configured
path that does not exist in the repository is skipped.

See [ENGINE.md](ENGINE.md) for the argv these keys produce.

### `publish`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `dry_run` | boolean | no (default `false`) | Run the whole pipeline and post nothing, logging the comment that would have been written. |

Optional, and the default is to post. Unlike the plan token counts this is
not a guess an operator has to make: a dry run spends exactly what a real
review spends, so defaulting to one would burn the allowance and show
nobody the result.

`dry_run` is validated as strictly `true` or `false` rather than cast. Every
non-empty string is truthy in Python, so `dry_run: "no"` would read as "post
for real" under a cast and as "post nothing" under YAML's own boolean rules;
refusing both is the only answer that cannot surprise an operator.

Reloadable on `SIGHUP` — see [Reload](#-reload) — and described in full in
[PUBLISHER.md](PUBLISHER.md#-publishdry_run).

### `logging`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `level` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` | no (default `INFO`) | How much the daemon says. Case-insensitive; an unrecognised name is refused at startup. |

Optional, and the whole section. There are **no destinations** and **no
per-logger map** — the daemon writes one stream to stderr and the supervisor
owns where it goes. [LOGGING.md](LOGGING.md) has the reasoning for both
omissions.

This is the *lowest* of three layers. Precedence is **flag > environment >
this file**:

```bash
pr-review-agent daemon start --log-level DEBUG   # highest
PR_REVIEW_AGENT_LOG_LEVEL=DEBUG                  # what a systemd unit uses
# then logging.level in config.yaml, then the INFO default
```

The environment layer is the one deployment uses. `GITHUB_TOKEN` already
arrives that way, so a unit already has an `Environment=` block and the level
lands beside it without touching `ExecStart=`.

**The level is applied to the `pr_review_agent` logger, not to the root one.** That is what stops `--log-level DEBUG` turning on the HTTP transport:

| `level` | `pr_review_agent` | `httpx` | `httpcore` | `asyncio` |
| :-- | :-- | :-- | :-- | :-- |
| `DEBUG` | `DEBUG` | `WARNING` | `INFO` | `DEBUG` |
| `INFO` | `INFO` | `WARNING` | `INFO` | `INFO` |
| `WARNING` | `WARNING` | `WARNING` | `WARNING` | `WARNING` |
| `ERROR` | `ERROR` | `ERROR` | `ERROR` | `ERROR` |
| `CRITICAL` | `CRITICAL` | `CRITICAL` | `CRITICAL` | `CRITICAL` |

Each third-party logger follows the configured level *downwards* without
limit, and upwards only as far as its own floor. One rule sets all three
floors — **the lowest level at which that library is quiet** — and it gives
three different answers, because the three get loud at three different
levels:

| Library | What it emits | Floor |
| :-- | :-- | :-- |
| `httpx` | Nothing at `DEBUG`; one line per request at `INFO` (`HTTP Request: GET https://… "HTTP/1.1 200 OK"`), and the poller makes several every cycle | `WARNING` |
| `httpcore` | Nothing at `INFO`; fourteen records per request at `DEBUG`, including the response header list verbatim — and it is the half of the pair the credential travels through | `INFO` |
| `asyncio` | One record for the whole process (`Using selector: EpollSelector`), nothing per request, no credential near it | `DEBUG` — it simply follows |

So at the default `INFO` **all three are silent**, which is the noise this
replaces: `basicConfig(level=INFO)` configured the *root* logger, so `httpx`
put a line into the log on every poll request. And `--log-level ERROR`
silences their warnings too, which one shared pin at `WARNING` would not
have done.

`tests/test_logs.py` pins the whole table.

### What each level shows

The six events `docs/LOGGING.md` names are split by how often they fire:

| Level | What becomes visible |
| :-- | :-- |
| `ERROR` | Every caught exception: the engine failing to start, a review or a publish failing, the account's usage limit, a crashed worker |
| `WARNING` | …plus every budget refusal, lapsed lease and `SIGHUP` that needs a restart, and the banner saying the agent is about to spend |
| `INFO` *(default)* | …plus each review starting, its outcome, it being posted and what it left in the budget; startup, cold-start watermarks, `SIGHUP` reloads and resumed runs |
| `DEBUG` | …plus every poll cycle and every trigger decision, the engine's argv and the `dry_run` review body |

Events 1 and 2 — the poll cycle and the trigger decisions — fire for every
pull request and every comment on every cycle, which is what made the v0.12.0
log unreadable, so they are `DEBUG`. Events 3 to 6 fire once per review and
are all visible by default, so the default level is the review lifecycle and
nothing else.

The `ERROR` row is a rule, not a list: a record emitted from inside an
`except` block is `ERROR`, and the test suite fails if one is not. That is
what makes `journalctl -p err` mean *something went wrong* rather than
*something was written*.

Not reloadable on `SIGHUP`: only `budget` and `publish` are, and both are
brakes. Changing the level needs a restart.

## 📄 A minimal file

Every key below is required; everything else has a default. This is
[`config.minimal.example.yaml`](https://github.com/prasadtalasila/pr-review-agent/blob/main/config.minimal.example.yaml) verbatim, and
`tests/test_config.py` loads it, so it cannot drift. The shipped file carries
no comments: it is meant to be copied and edited, and the reasoning belongs
on this page rather than in a file that becomes somebody's `config.yaml`.

The agent's own id appears twice: once as `agent_user_id`, and once in the
allowlist. The second is belt and braces — the `self_author` /
`self_commenter` checks run *before* the allowlist is consulted, so that
entry is never reached — and it is there so the list reads as the complete
set of accounts the deployment knows about.

```yaml
github:
  repo: prasadtalasila/pr-review-agent
  agent_user_id: 9206466

triggers:
  allowlist:
    - 114395272
    - 9206466

budget:
  session_tokens: 88000
  weekly_tokens: 1500000
  max_run_tokens: 60000
```

An earlier version of this page showed a minimal file with no `budget`
section at all. It would not have loaded — `budget` is required, and its
three token counts have no defaults on purpose.

## 🔁 Reload

`SIGHUP` re-reads `config.yaml` and adopts its `budget` section, so stopping
the agent never requires a restart.

**Only `budget` is hot-swapped.** A changed `github`, `triggers`, `store`,
`workspace` or `worker` section is logged as needing a restart rather than half-applied:
the daemon's watermarks describe the repository it started against, and
swapping that mid-flight would make them meaningless. `workspace.cache_dir`
is in that list for a neighbouring reason — moving the cache under a running
daemon would orphan the mirror it is fetching into.

The checkout's two caps *are* reloaded, because they are `budget` keys. That
is why the workspace is handed them per checkout rather than reading them
once at startup: a copy taken at construction would ignore the reload
silently.

**A broken file leaves the previous configuration in force**, logged at
`ERROR`. Crashing on a bad reload would turn the emergency brake into a way to
take the service down with a typo.

`publish.dry_run` is the other reloadable key, and it took this mechanism
without change: `SIGHUP` swaps the `budget` and `publish` sections together,
and a broken file leaves both as they were.

It is the quieter brake. `budget.enabled: false` stops the agent *spending*;
`publish.dry_run: true` lets it spend exactly as before and posts nothing,
logging the comment it would have written instead. Use it to watch what the
agent would say before letting it say it — not to save money. See
[PUBLISHER.md](PUBLISHER.md#-publishdry_run).
