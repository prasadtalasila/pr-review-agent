# Publisher

The last step between a computed review and a visible one. It does two
things, minutes apart, and the gap between them is most of the design. It is
also where the agent is kept from summoning itself.

Source: `src/pr_review_agent/publisher.py`, `src/pr_review_agent/runs.py`.

## 👀 The acknowledgement is immediate

A 👀 reaction goes out as soon as the trigger is **claimed** — before the
pull request is read, before the checkout, before any engine runs. A review
takes minutes; an acknowledgement that waited for one would be useless.

It lands on whatever the contributor actually touched:

| Trigger | Reaction endpoint |
| :-- | :-- |
| `mention` in a conversation comment | `/issues/comments/{id}/reactions` |
| `mention` in an inline diff comment | `/pulls/comments/{id}/reactions` |
| `pr_opened` | `/issues/{n}/reactions` |

The two comment endpoints draw their ids from different sequences, so
knowing the id is not enough — posting to the wrong one 404s, or reacts to
an unrelated comment. Which endpoint a comment arrived on is recorded as
`CommentSource` when the poll payload is mapped, where it is free to learn,
and carried on the `Trigger` and the queue row through to the claim.

**It never raises.** A GitHub failure here is logged and the review proceeds:
losing a courtesy must not cost a review the governor has already reserved
allowance for. The `except` is narrowed to `GitHubClientError` on purpose —
a bug in the publisher *should* surface rather than hide behind a missing
emoji.

**A dry run still acknowledges.** The reaction says "the agent has your
trigger", which is true in a dry run and is the one thing an operator
watching one still wants a contributor to see.

### About the 15 second criterion

[STATUS.md](STATUS.md) asks for an acknowledgement within 15 s **of the
trigger being seen**, not of it being written. The poll interval is adaptive
10–600 s ([POLLER.md](POLLER.md)), so nothing here can beat the polling
latency — the acknowledgement is what makes that latency *feel* short, which
is exactly why [DESIGN.md](DESIGN.md#-alternatives-considered) could reject a
lower-latency relay design for it.

## 🔁 The head is re-read immediately before posting

A review describes one commit. By the time it finishes the pull request may
have moved on, and a review of a superseded commit landing late is worse
than no review at all.

So the publisher re-reads `GET /pulls/{n}` and compares the live head with
the one the review ran against. On a mismatch it posts nothing and the queue
row is **completed**, not retried: a push to an existing pull request is not
a trigger ([TRIGGERS.md](TRIGGERS.md)), so another attempt would re-read the
same stale sha and reserve allowance to do it.

[QUEUE.md](QUEUE.md#-what-the-queue-does-not-do) assigns this check here
deliberately. It has to be a live read taken as late as possible; at claim
time it would be worth nothing.

The read happens in a dry run too. An operator watching one needs to see the
same decision the real path would take, not a shortcut past it.

## 💬 One comment per pull request

Findings are posted as a single ordinary issue comment on the pull request's
conversation, and a re-review **edits that comment in place** rather than
adding another. What a reader sees is the agent's current opinion, not a
thread of superseded machine opinion they have to scroll past. The history
is not lost — it lives in `runs` and the ledger, where it can be queried and
purged.

The body names the commit it describes, because an edited comment otherwise
says nothing about which revision it is about. It also names the round and
the commit count — "round 3" and "round 1" are different statements, and a
reader returning to an edited comment cannot otherwise tell which one they
are looking at. The commit count comes off the live pull request payload the
publisher already reads to decide whether the head moved, so it costs no
extra round trip and no database column.

Findings render in a pinned section-then-number-then-location order, so a
re-review that finds the same things produces the same body below the header
and the edit is a no-op.

### The rendered report

[`reporting/review-report.md`](reporting/review-report.md) is the contract;
this is the summary.

Findings are grouped under three headings and numbered across the whole
report:

| `Severity` | heading |
| ---------- | ------- |
| `blocker` | Blocking |
| `major`, `minor` | Should fix |
| `nit` | Nits |

`Severity` itself is **unchanged**. It is persisted in the `runs` table and
asserted across the suite, so it is not collapsed to three values — but a
`major` finding that is not a blocker must not print under a heading claiming
it blocks, which is why the two middle levels share one.

Numbers are stable for the life of the pull request. A finding carried over
from an earlier round keeps its number; a finding that gets fixed leaves its
number vacant, and the gaps are never closed. `1, 3, 5` says two earlier
items were dealt with without spending a word on it, and renumbering would
silently relabel items a reader had already referred to. See
[WORKER.md](WORKER.md) for where the numbers are assigned.

Nits render as prose rather than numbered entries: a nit that deserves its
own entry is not a nit. A finding carries no `path:line` anchor — the paths
that matter are the ones the reviewer names in its own prose, and the
location stays on the stored `Finding` for a future line-anchored comment.

## 🛡 The publisher cannot approve anything

[DESIGN.md](DESIGN.md#-prompt-injection-is-in-scope) names three
prompt-injection mitigations, none of which relies on the model behaving.
This is the third: **whatever a review concludes, the agent takes no
approval action and no merge action.**

It is held by an **absent capability** rather than a guarded field. The only
GitHub writes this module knows how to make are a reaction and an ordinary
issue comment. There is no event field to set wrongly, no severity that can
escalate, and no branch to get wrong — a pull request whose text asks to be
approved cannot get what it asks for even if every other mitigation fails
and the model does exactly as it is told.

Two tests pin it: one asserts on the recorded requests that no path matches
`/reviews` and no body carries an `event` key; the other reads the module's
own source and asserts it contains no token that could change that. A future
change that reaches for the reviews endpoint has to delete a test to do it.

This is why there is no line-anchored inline review. Inline comments would
mean the reviews endpoint, which would mean an `event` field, which would
mean the guarantee became "we always set it to `COMMENT`" — a promise about
code rather than a property of it.

## 🔁 Nothing it posts can summon another review

A review body is engine prose over the tree being reviewed. When that tree is
*this* repository, the prose names `@claude` readily — the handle is what the
whole trigger pipeline is about, so a finding about that pipeline quotes it.
The comment is also edited in place on re-review, which bumps `updated_at`,
so the poller sees it as fresh every round.

Left alone that is a loop: the agent posts, the classifier accepts what it
posted, and the agent reviews the pull request again. The dedupe key bounds it
to one extra paid review per pull request, because the comment id does not
change — one more than anybody asked for.

So `render` returns `triggers.mention.neutralise(body, handle)`. It rewrites
the `@` of exactly the mentions `has_mention` would find into `&#64;`, which
GitHub renders as `@`: the reader sees no difference, and the raw body a later
poll reads back has no `@` for the detector to match.

Two details are deliberate:

- **Only mentions in prose are touched.** A handle inside a code span or a
  fence is left exactly as it was written. The detector already ignores those
  regions, so there is nothing there to neutralise — and GitHub renders no
  entity inside them, so an escape would show the reader `&#64;claude` in what
  is meant to be code.
- **The escape is an entity, not backticks.** Wrapping the handle in a code
  span was the cheaper way to reach the same detector rule, and it is not
  safe: the body is engine output over an untrusted tree, so it can carry an
  unmatched backtick that pairs with the opening one and leaves the handle in
  prose after all.

This is where the loop is closed, which is why the classifier needs no notion
of who the agent is — see
[TRIGGERS.md](TRIGGERS.md#-the-agent-cannot-summon-itself) for what that
bought and what it widened.

## 🧯 Nothing it posts can act

Closing the self-mention loop handles one handle: the agent's own. Everything
else in a finding was still written through verbatim, and a comment posted by
an account with a real token is not inert text. GitHub gives ordinary comment
markdown four ways to act, and `sanitise.py` answers the first three:

| In engine prose | What GitHub does with it | What is posted |
| :-- | :-- | :-- |
| `@someone`, `@org/team` | notifies that account or team, **from the agent** | `&#64;someone` |
| `#123`, `owner/repo#123` | cross-references that issue, from the agent | `&#35;123` |
| `GH-123` | the same, in the other spelling | `GH&#45;123` |
| `<sub>`, `<!-- -->` | renders — a forged trailer, or text hidden from a reader | `&lt;sub>` |

Escaped rather than stripped: a reader still sees `@someone` and `#123`,
because "see #123" is saying something worth reading. What it stops being is a
link the agent pulled. And prose only, for the same two reasons `neutralise`
gives — GitHub does not mention, cross-reference or render HTML inside a code
span or a fence either, and an entity there would show the reader `&#64;` in
what is meant to be a code sample. `strip_non_prose` is shared between the two
modules, so they cannot disagree about where prose is.

Bare commit shas are **not** escaped. They auto-link to a commit in the
repository the review is posted on: a link, not a notification and not a
cross-reference somewhere else.

**The fourth way is length.** GitHub rejects a body over 65 536 characters
with a `422` — *after* the review was paid for — and the worker would then
re-offer the same body on every claim forever. `render` caps at
`MAX_BODY_CHARS` (60 000, with the margin absorbing the header, the trailer
and the entities escaping adds) and drops whole sections to get there,
**lowest severity first**, because `SECTIONS` is already in the order a reader
needs them. Only when the highest-severity section alone is over the limit is
prose cut mid-sentence. Either way the body says so, so a reader is never
shown a partial review that looks complete.

**And a canary.** Before anything is posted, the body is searched for the
values in `Publisher.secrets` — the live `GITHUB_TOKEN`, today. On a hit the
publish is refused (`PublishOutcome.REFUSED`), logged at ERROR *without* the
body, and stamped so it is not offered again: re-running would spend again to
render the same comment. The canary cannot catch an encoded secret. It catches
the straightforward one, and turns a silent leak into an alert.

## 💾 A paid review is kept until it can be posted

The engine is the only irreversible step, so the order after it is fixed:

```text
settle  →  record  →  publish  →  finish
```

Recording the findings in `runs` **before** publishing is what makes a failed
publish cheap. The review row is completed — the review itself is done and
must never be run again — and what remains, the posting, is enqueued as a
`publish` work item naming that run. Draining it reaches no engine at all, so
a flaky GitHub write cannot spend the allowance a second time. A test pins
the engine call count at 1 across a failed publish and its successful retry.

**Republication is its own unit of work.** It used to be a bypass inside
`admit`: any claim for a pull request carrying an unposted run was let
through without reserving, and the claim was then spent posting *the oldest*
such run rather than doing what it was claimed for. So a fresh `@claude` was
not a review request any more — it was whatever run happened to be pending,
and a run that could never be posted swallowed every later trigger on that
pull request. Now `admit` has one meaning, a claim for a review always
reserves, and a mention always reviews the head it was written against.

The item is an **ordinary queue row**, so it takes the ordinary
per-pull-request lease. That keeps one pull request in one worker's hands; a
second lease would be a second chance to post the same comment twice.

**It reserves nothing and settles nothing.** The worker's `admit` predicate
consults the governor for everything that could spend, and exempts a
`publish` item — read off the item's own kind, never off what some other row
left behind. The run it names reached an engine once, under a reservation
that has already settled; posting it reaches none. Without the exemption an
exhausted budget would hold a review the allowance was *already spent on*
hostage until a window rolled — refusing to spend nothing, to avoid a cost
paid days ago. And because the ladder's `mention_only` rung refuses a
`pr_opened` trigger from 85% utilisation, not 100%, that hostage-taking would
start well before the budget was gone.

This is why `CLAUDE.md` §5's rule is stated about **engines** rather than
about claims. Nothing reaches a review engine outside the governor; a
publish-only claim reaches no engine, writes no ledger row, and is pinned by
a test asserting both.

**The lease is re-checked immediately before the write.** The owner guards on
`complete` and `release` run *after* it — late enough to discard a row, too
late to unsay a comment.

**A failed post hands the row back unattempted.** `max_attempts` caps what one
poison trigger may drain from the allowance, and a post that reached no engine
drained nothing. Counting it would abandon a review after three failed posts
and leave its findings recorded and permanently invisible — which is exactly
what would have happened before `release_unattempted` existed.

| Publish outcome | Queue verb | Why |
| :-- | :-- | :-- |
| published | `complete` | Done. |
| dry run | `complete` | The pipeline ran; there is nothing to retry. |
| superseded | `complete` | The head will never match again. |
| write failed, engine ran | `complete`, plus a `publish` item | The review is done; only the posting is outstanding. |
| write failed, `publish` item | `release_unattempted` | Retryable, and the attempt does not count: nothing was spent. |
| run already posted or purged | `complete` | Nothing left to post, so the item is finished. |

A publish-only retry therefore never exhausts the attempt bound. That is
deliberate — the bound measures allowance drained, and this drains none — but
it does mean a comment GitHub will *never* accept is retried for as long as
the item is claimable. The retention sweep is where that eventually stops
mattering: a purged run is no longer offered for publication, and the item
closes itself the first time it finds nothing to post.

## 🧪 `publish.dry_run`

Runs the whole pipeline and posts nothing, logging the comment it would have
written. It is reloadable on `SIGHUP` through the mechanism
`budget.enabled` built — a brake that needs a restart is not one — and a
broken configuration file leaves the previous value in force.

**It does not make a run free.** The engine has already run by the time the
publisher is asked, so a dry run spends exactly what a real review spends.
The brake that stops spending is `budget.enabled`. See
[CONFIG.md](CONFIG.md).

A dry run still stamps the run as needing publication no longer. The pipeline
ran and there is nothing left to post; an unstamped run would be re-offered
on every claim for the lifetime of the database.

## 📋 The `runs` table

See [STORAGE.md](STORAGE.md#-schema) for the columns. Four decisions worth
knowing:

- **Keyed on `dedupe_key`**, matching `queue` and `ledger`, which is what
  turns "every posted comment is traceable to a ledger row recording engine,
  model, mode, usage and `usage_confidence`" into a query rather than a
  convention.
- **`comment_id` is written per run and read per pull request.** The question
  at publish time is never "what did this run post" but "what does this pull
  request already have".
- **`findings` is JSON text, not a child table.** Written once, read once,
  purged wholesale; nothing queries a finding by path, line or severity.
- **`content_purged_at` is stamped apart from emptying `findings`,** so a run
  whose content was deleted after a merge stays distinguishable from a run
  that looked and found nothing — the same distinction `Outcome` keeps
  between `truncated` and a clean empty result.

`RunStore.purge_content` exists and has no caller. The retention sweep is
specified in terms of this table, and deciding the purge's shape later would
mean deciding it against rows already written the wrong way.

## 🔑 Prerequisites

The GitHub token needs **write** scope -- Pull requests read/write and Issues
read/write, the latter because a pull request's conversation comments are
*issue* comments in the REST API ([TOKENS.md](TOKENS.md#-the-github-token)).
`DESIGN.md` recorded that read-only sufficed for polling and that write scope
was only needed once the publisher existed; it exists, and the failure mode
without it is a review that is polled for, claimed, paid for and computed, and
then 403s on the last call.

`pr-review-agent host check` reports on this and cannot settle it. The only
signal the repository response carries is `permissions.push`, which is
*contents: write* -- so a correctly scoped fine-grained token reads as `false`
there, and the check says so as a caution rather than refusing to start.

Nothing else. `github.agent_user_id` used to be a prerequisite here, so the
agent's own comment would be classified `self_commenter`; that key is gone,
and [this module is what replaced
it](#-nothing-it-posts-can-summon-another-review).
