# The engine seam

The one place a different coding agent plugs in. Everything else — polling,
allowlisting, dedupe, leasing, the budget windows, publishing — is
agent-agnostic, so only "run a review" is swappable.

This page documents the seam and the one adapter that implements it. The
adapter **can spend money**, and since the [review worker](WORKER.md) landed
it has a caller: the worker claims a row, checks the code out and runs the
engine the configuration names. For why the design has a seam at all, see
[DESIGN.md](DESIGN.md#-generalisation-to-other-agents).

## 🔌 The protocol

```python
class ReviewEngine(Protocol):
    name: str

    @property
    def capabilities(self) -> Capabilities: ...

    async def review(self, request: ReviewRequest) -> ReviewResult: ...
```

Async, because every adapter will be waiting on a subprocess and the daemon
and [workspace](WORKSPACE.md) already are.

`name` is recorded on the ledger row, so a posted comment is traceable to
what produced it.

## 📥 What an engine is given

| Field | Type | Why it is there |
| :-- | :-- | :-- |
| `checkout` | `Checkout` | The tree on disk, its `head_sha`, the merge base, and the diff. |
| `facts` | `PullRequestFacts` | Number, base ref and the size counts the caps were measured against. |
| `trigger` | `Trigger` | What asked for this review — an opened pull request or a mention. |
| `mode` | `Mode` | The rung of the [degradation ladder](BUDGET.md) the run was admitted under. |

The diff is **not** passed separately. It is already on the `Checkout`, and
two copies of one string are two things that can disagree.

`mode` is passed rather than inferred so an engine can spend *less* under
`mention_only` — fewer turns, a tighter prompt — instead of the governor's
only lever being to refuse the run outright.

**The tree and the diff are untrusted input.** An engine may read them and
must not let them widen what it is allowed to do. The rule is the same one
that governs the allowlist; see [DESIGN.md](DESIGN.md#-prompt-injection-is-in-scope).

## 📤 What an engine returns

`ReviewResult` is findings plus usage.

A `Finding` is `path`, `line`, `severity`, `body` — the four fields a
line-anchored review comment needs. Nothing more, because the publisher does
not exist yet and its data model is not this change's to design. `Severity`
is advisory in the strongest sense: the publisher posts event `COMMENT`
whatever a review concludes, so not even `blocker` can block a merge.

`outcome` is how the run ended: `completed`, `truncated` or `failed`. It
exists because a run that was cut off and a run that cleanly found nothing
produce the same empty `findings` tuple, and only one of them reviewed the
pull request. `ReviewResult` refuses to carry findings on any outcome but
`completed`, so "the publisher may post these" is a property of the seam
rather than a rule each adapter is trusted to follow.

Usage is carried on every outcome, including the failed ones. The money is
spent whatever the run concluded, so it still has to settle.

`usage` is `budget.Usage` itself, not a parallel type. That is what makes
"a result carries everything `Governor.settle` needs" true by construction
rather than by review — `settle` takes exactly that object. Two invariants
are enforced in `__post_init__`:

- `usage_confidence: unavailable` may not also report a token count. An
  unknown cost and a number are different answers, and a ledger row claiming
  both would let a run that measured nothing still draw down a window.
- `usage.engine` must be set. An unattributable cost is not settleable.

## 🎚 Capabilities

Five booleans, none with a default, because a default would be a claim no
adapter actually made.

| Flag | Consumed by | Meaning |
| :-- | :-- | :-- |
| `structured_output` | the prompt contract | Whether the engine can be schema-constrained. If not, the adapter needs a fenced-JSON contract with a *budgeted* validation retry. |
| `usage_reporting` | the governor | Whether the engine reports what a run cost. |
| `read_only_sandbox` | — | Whether the engine can be confined to reading. |
| `subagents` | — | Whether the engine can fan out. |
| `prompt_caching` | — | Whether repeated context is billed once. |

The last three have no consumer yet. They are declared because
[DESIGN.md](DESIGN.md) names them and because an adapter's answer is knowable
when the adapter is written rather than months later.

### `usage_reporting: false` is the interesting one

An engine that reports no tokens forces the governor onto **proxy controls
only** — run count, wall clock, turn caps. That is a materially weaker
guarantee than a token count: the windows stop measuring spend and start
measuring activity. It is why every ledger row already records a
`usage_confidence` of `exact` / `estimated` / `unavailable`, and why the
capability is declared in advance instead of being discovered from the first
run that reports nothing.

The conformance suite enforces the pairing: an engine whose capabilities say
`usage_reporting: false` must settle as `unavailable`, and one that says
`true` must not.

## 🧪 `FakeEngine`

Canned findings, a canned `Usage`, a recorded list of every request it was
handed, and no I/O of any kind. It is what lets the review step be tested
without a network, an API key or a token — the seam's most immediate return,
before any adapter exists.

`tests/test_engine.py` holds a conformance suite parametrised over a list of
engines. `ClaudeCliEngine` joins it with its subprocess stubbed out, because
every rule in the suite is about what an engine *returns*. A second adapter
joins the same list rather than growing its own copy of the rules.

## 🖥 `CliEngine`: the subprocess boundary

The part that is the same for `claude`, `codex` and `opencode`: launching the
tool with `cwd` set to the checkout, an environment built rather than
inherited, a wall clock it cannot outlive, and the error vocabulary —
`EngineUnavailable`, `EngineTimeout`, `EngineProtocolError`. A subclass
supplies the argv, the prompt, the parser and the capabilities.

**The environment is an allowlist** — `PATH`, `HOME`, and whatever name
prefixes an adapter declares. `os.environ` minus a denylist fails open the
moment a new variable appears, and the variable that must never reach this
process is the agent's GitHub credential: a process reading an attacker's
tree and feeding a public comment is the wrong place for it. A test asserts
`GH_TOKEN` is absent by value.

**The prompt goes on stdin**, not in the argv. It carries the diff, and a
diff-sized argv hits the platform limit on exactly the pull requests that
most need reviewing. It also keeps the argv small enough to log whole, which
is what makes a posted review traceable to the invocation that produced it.

Untrusted text inside the prompt is fenced, and the fence is **sized to its
contents**: the builder finds the longest run of backticks in the diff and
opens with one more than that. A fixed three-backtick fence is closable by
any diff that contains one, which would let the "this is data" boundary be
ended by the data.

A timeout kills the process before it prints anything, so `EngineTimeout`
carries no usage while the governor is still holding a reservation. The
honest settlement for such a run is the full reservation at `unavailable`
confidence; that is the worker's call, and the adapter only guarantees the
failure is distinguishable.

## 🤖 `ClaudeCliEngine`

```text
claude -p --output-format json --json-schema <schema> --model <configured>
  --system-prompt <agent-authored> --tools Read,Grep,Glob --restricted
  --setting-sources '' --strict-mcp-config --disable-slash-commands
  --no-session-persistence --permission-mode dontAsk --permission-prompts none
```

Three of those are the injection defence, and they are argv a test asserts
rather than prompt wording a model can be talked out of:

- `--tools` names the whole tool set, and it is read-only. No Write, no Edit,
  no Bash.
- `--restricted` removes the command-running tools a second time and ignores
  user, project and local settings files — so a `CLAUDE.md` in the tree under
  review is not instructions to the reviewer.
- `--permission-prompts none` means nothing can escalate by asking.

`--bare` would give the same isolation and was rejected: it forces
`ANTHROPIC_API_KEY` authentication, which would silently settle the billing
question [DESIGN.md](DESIGN.md#-billing-mode) leaves open.

| Capability | Value | Because |
| :-- | :-- | :-- |
| `structured_output` | `true` | `--json-schema` validates the output and re-prompts on mismatch |
| `usage_reporting` | `true` | the envelope reports tokens |
| `read_only_sandbox` | `true` | `--tools` plus `--restricted` |
| `subagents` | `false` | the CLI can fan out; this tool set does not let it |
| `prompt_caching` | `true` | the envelope reports cache tokens |

### Reading the envelope

Stdout is one JSON object, and the parse is strict:

| Envelope | Result |
| :-- | :-- |
| `subtype: success` with `structured_output` | findings, `completed` |
| `subtype: success` without it | no findings, `failed` |
| `subtype: error_max_structured_output_retries` | no findings, `failed` |
| `subtype: error_max_turns` | no findings, `truncated` |
| any other subtype, or `is_error` | no findings, `failed` |
| not JSON, or not `type: result` | `EngineProtocolError` |

An unreadable envelope raises rather than being reported as an empty review.
Parsing what a CLI prints is the cost of the subprocess boundary, and a
format change that silently became "no findings" would look exactly like a
clean review.

`usage.tokens` is every `*_tokens` field the envelope reports, cache reads
included: cheaper than fresh input, not free, and the windows measure what a
run consumed. `total_cost_usd` is on the envelope and deliberately not
recorded — the windows are token-denominated.

#### The schema retry is inside the CLI, and it spends

`--json-schema` does not merely validate. When the model's output does not
fit the schema the CLI **re-prompts by itself**, and only gives up with
`error_max_structured_output_retries`. That retry is not a lever this adapter
holds: there is no flag to disable it and no callback before it fires.

Two consequences worth stating rather than discovering from a ledger row:

- A single `review()` call can cost several model turns. The reported `usage`
  covers all of them, so the ledger stays honest — but a run's cost is not
  bounded by one turn's worth of tokens, and the pre-flight estimate should
  not be read as though it were.
- `error_max_structured_output_retries` is the **expensive** failure: it is
  the outcome that spent the most and produced the least. It maps to
  `failed` rather than `truncated` deliberately — retrying it costs the same
  again with no reason to expect a different answer.

Keeping `FINDINGS_SCHEMA` small and flat is therefore a spending decision,
not a style one. Every required field is another way for a run to end in the
retry path.

#### The sizes in the prompt are the reviewable ones

The prompt quotes `checkout.reviewed` — what survived `budget.excluded_paths`
— and not `PullRequestFacts`' API totals. The diff the engine is shown has
already had the excluded paths removed, so quoting the API's figures would
tell the reviewer it is missing files that were withheld on purpose, and
invite it to go looking for them with the tools it does have.

### Standards come from the base ref

The engine is generic; the standards are per-repository. They are read at the
checkout's **merge base** rather than its head, so opening a pull request
cannot rewrite the reviewer's instructions, and read through the run
worktree's shared object database, so it costs no second fetch. What that
trusts is stated in [DESIGN.md](DESIGN.md#-prompt-injection-is-in-scope):
anyone who can merge to the base branch. Configured in
[CONFIG.md](CONFIG.md)'s `engine.standards_paths`.

### Version

`claude --version` runs once before the first review. A mismatch against
`engine.expected_version` **warns and proceeds**: the parse is what actually
protects the run and it already fails loudly, while refusing would take the
reviewer offline on a routine upgrade that changed nothing we read.

## 🧵 Who calls it

The [review worker](WORKER.md), and nothing else. It builds the
`ReviewRequest` from a claim, hands it over, and treats **any** exception the
engine raises as one retryable fact: the run produced no review. An adapter
therefore does not need to classify its own failures for the caller's
benefit — it needs to fail rather than return an empty review.

What the worker does need is an honest `usage`: it settles the ledger with
exactly what `ReviewResult.usage` reports, and a run that fails after the
engine started is charged its full reservation.

## 🚧 What lands next

The budget pieces that need a *running* engine — the circuit breaker, and
layer 3's per-run ceilings, for which `--max-budget-usd` and the turn caps
are the levers — then the worker that drains the queue, and then the
publisher.

Layer 2 is not among them: path exclusions and the
[pre-flight estimate](BUDGET.md#-the-pre-flight-token-estimate) needed only a
diff, so they landed alongside the adapter rather than behind it. An engine
is handed a diff with excluded paths already absent, and a run predicted to
cost more than `max_run_tokens` never reaches one. The adapter's part is to
report usage honestly, which is what keeps the estimate's fit calibrated — an
engine declaring `usage_reporting: false` produces rows the fit deliberately
ignores.
