# The engine seam

The one place a different coding agent plugs in. Everything else — polling,
allowlisting, dedupe, leasing, the budget windows, publishing — is
agent-agnostic, so only "run a review" is swappable.

This page documents the seam. **No adapter exists yet**: nothing in
`pr_review_agent.engine` spends a token, opens a socket or claims a queue
row. For why the design has a seam at all, see
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
engines that currently has one entry. A second adapter joins that list
rather than growing its own copy of the rules.

## 🚧 What lands next

A `claude` CLI adapter: `claude -p --output-format json`, run as a
subprocess with `cwd` set to the checkout, a timeout, and a scrubbed
environment. With it come the budget pieces that need a running engine — the
circuit breaker, layer 3's per-turn enforcement and the pre-flight token
estimate — and then the publisher.
