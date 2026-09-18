# The review engine seam, and a required `agent_user_id`

Design for issues [#14](https://github.com/prasadtalasila/pr-review-agent/issues/14)
and [#15](https://github.com/prasadtalasila/pr-review-agent/issues/15),
shipped as one pull request. Issue
[#12](https://github.com/prasadtalasila/pr-review-agent/issues/12) is
resolved by the decision recorded below rather than by the research it asked
for.

## The decision that came first

**Every review engine is a command-line tool, invoked as a subprocess. No
vendor SDK is linked.** `claude`, `codex`, `opencode` and the rest are
reached through their CLIs.

This was an instruction, not a finding, but it settles #12. That issue exists
because the Claude Agent SDK documentation forbids third-party developers
offering claude.ai login or rate limits for products "built on the Claude
Agent SDK". An agent that links no SDK is not such a product: it is a *user*
of Claude Code, and it offers nobody a login or a rate limit — the credential
never leaves the host.

What survives is the older, narrower question, which the SDK note never
answered: whether an unattended daemon running `claude -p` over other
contributors' pull requests is ordinary individual use of a personal Max
subscription. That is recorded in `DESIGN.md` as still to confirm against the
Commercial Terms. It does not block the seam, because the seam is
authentication-agnostic.

Three reasons the CLI-only rule is also the better engineering choice, all
recorded in `DESIGN.md`:

1. A CLI is the interface every one of these agents actually offers. An
   SDK-shaped seam would be a seam only Claude fits through.
2. A subprocess is a containment boundary a library call is not — its own
   working directory, its own environment, a kill-on-timeout. The review step
   runs over an untrusted tree.
3. An SDK pins a vendor's transitive dependency tree into a daemon whose
   other dependencies are `httpx`, `PyYAML` and the standard library.

The cost is parsing CLI output, so an output-format change breaks an adapter
where a typed SDK response would not. Each adapter pins the version it was
written against and fails loudly rather than reporting an empty review.

## Scope

In: the protocol, the records it passes, a fake engine, and the documentation
the decision invalidates. Also #14, which is unrelated in subject but is a
config-file correction of the same size.

Out, explicitly: any adapter that spends anything; anything that drains the
queue; any change to budget behaviour. `CLAUDE.md` §5's spending rule is not
engaged, because nothing here can call a review engine at all.

## The seam

`src/pr_review_agent/engine/`, documented in `docs/ENGINE.md`.

```python
class ReviewEngine(Protocol):
    name: str

    @property
    def capabilities(self) -> Capabilities: ...

    async def review(self, request: ReviewRequest) -> ReviewResult: ...
```

Async, because every adapter waits on a subprocess and the daemon and
workspace already are.

### `ReviewRequest`

`checkout`, `facts`, `trigger`, `mode`.

Issue #15 lists "the checkout, the diff" separately. The diff is already on
`Checkout`, and two copies of one string are two things that can disagree, so
it is passed once. A test pins the absence.

`mode` is the ladder rung the run was admitted under, passed rather than
inferred so an engine can spend *less* under `mention_only` instead of the
governor's only lever being to refuse the run.

### `ReviewResult`

`findings: tuple[Finding, ...]` plus `usage: budget.Usage`.

Reusing `Usage` rather than defining a parallel type makes #15's acceptance
criterion — "carries everything `Governor.settle` needs" — true by
construction: `settle` takes exactly that object. A test runs enqueue →
claim → review → settle against a real store to prove it.

Two invariants in `__post_init__`:

- `usage_confidence: unavailable` may not report a token count. An unknown
  cost and a number are different answers, and a row claiming both draws down
  a window on a measurement nobody made.
- `usage.engine` must be set. An unattributable cost is not settleable.

The complementary rule — an engine declaring `usage_reporting: false` must
settle as `unavailable` — lives in the conformance tests, because a result
object cannot see its engine's capabilities.

### `Finding`

`path`, `line`, `severity`, `body`. Four fields, because the publisher does
not exist and its data model is not this change's to design. `Severity` is
advisory: the publisher posts event `COMMENT` whatever a review concludes, so
not even `blocker` can block a merge.

### `Capabilities`

All five flags `DESIGN.md` names, none with a default — a default would be a
claim no adapter made. `read_only_sandbox`, `subagents` and `prompt_caching`
have no consumer yet; the docstring says so rather than implying otherwise.
They are declared because an adapter's answer is knowable when the adapter is
written.

### `FakeEngine`

Canned findings, canned usage, a recorded list of requests, no I/O. It is the
seam's immediate return: the review step becomes testable with no network, no
key and no token. `tests/test_engine.py` parametrises a conformance suite over
a list of engines that currently has one entry; a second adapter joins the
list rather than growing its own copy of the rules.

## #14: `agent_user_id` becomes required

`GitHubConfig.agent_user_id` goes from `int | None` to `int`, with `bool`
rejected explicitly for the reason the token counts already reject it.

The justification is the same class of argument as the budget keys: a field
whose absence costs money is not optional. Without it the `self_author` /
`self_commenter` rejections never fire, so a posted review can re-trigger a
review of the same pull request — a loop only visible after it has spent.

`config.minimal.example.yaml` gains the key and loses every comment; it is
copied verbatim to `config.yaml`, and a comment there ages in a file nobody
reviews. The reasoning moves to `CONFIG.md`. A test asserts the file contains
no `#`.

`Classifier.agent_user_id` stays `int | None`. It is separately
constructible, its own tests rely on that, and widening #14 into it is the
unrelated refactor `CLAUDE.md` §3 rules out.

**Both ids in the shipped examples are placeholders.** `agent_user_id:
123456789` matches no real account and must be replaced with the reviewer
account's id before the agent posts anything.

## Verification

`tests/test_engine.py`, plus the updated `tests/test_config.py` and
`tests/test_bootstrap.py`. The whole suite is pure functions and a local
SQLite file: no network, no tokens. Then the full local gate from
`DEVELOPER.md`.
