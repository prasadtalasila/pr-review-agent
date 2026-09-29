# Publisher

The last step between a computed review and a visible one. It does two
things, minutes apart, and the gap between them is most of the design. It is
also where the agent is kept from summoning itself.

Source: `src/pr_review_agent/publisher.py`, `src/pr_review_agent/runs.py`.
The layout itself is `src/pr_review_agent/report.py`, split out so that it
imports nothing that talks to GitHub and can be copied into an installed
skill — see [the review skill](reporting/review-skill.md). `render`,
`refusal`, `TRAILER` and the rest are re-exported from `publisher`, so
every existing import still resolves.

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

## 🪧 The refusal notice

The gap the acknowledgement opens has to be closed at both ends. A 👀 goes
on as soon as the claim is made, minutes before anyone knows whether a
review is possible — and when the size gate or the
[pre-flight](BUDGET.md#-the-pre-flight-token-estimate) then refuses, the row
is abandoned. Until 1.4.0 that was the whole of the contributor's
experience: an acknowledgement, and then silence, with the reason visible
only in the operator's journal. A maintainer who typed the handle on a
6 000-line pull request had no way to learn that raising `max_changed_lines`
is the fix (issue #78).

`Publisher.notify` posts one short comment saying which refusal fired and
which setting would undo it. The sentence comes from whichever component
refused — `PullRequestTooLarge.notice` for the size gate,
`Governor.preflight` for the other two — because that is where the numbers
are, and a second copy of the rule here would drift.

**Only deterministic refusals are announced.** A transient failure is
retried and the review it eventually produces speaks for itself — until it
runs out of attempts, which has [a notice of its own](#-the-failure-notice); a closed
pull request is told nothing, because nobody is reading it; and a
`PayloadError` is a bug in the agent rather than something a contributor can
act on. What is left is the set somebody can actually do something about,
which is what makes a notice worth the comment it costs.

**It cannot accumulate.** One notice ends one trigger, and the row is
abandoned in the same breath, so nothing re-offers it. A second notice means
a second deliberate `@handle` — somebody asking again — and answering that
one too is the point rather than a leak.

**A dry run posts none**, unlike the acknowledgement. The reaction says
"your trigger arrived", which is true in a dry run; this writes a comment
under the agent's account, which is exactly what the brake is on to prevent.

**It never raises**, for the same reason the acknowledgement does not: the
row it explains is already closed and settled at zero, and letting a failed
courtesy propagate would turn a free refusal into a retried failure that
reserves allowance to reach the same answer.

**Its own advice is a mention**, so it goes through `neutralise` like every
other body — otherwise the comment telling a reader to type `@claude` again
would summon the review it is explaining the absence of. See
[below](#-nothing-it-posts-can-summon-another-review).

## 🧯 The failure notice

An engine that fails on every attempt used to end the same way a refusal
did: a 👀, then silence. `Publisher.report_failure` closes that gap with
one comment, posted when the **last** attempt `queue.max_attempts` allows
fails — never on an earlier one, because the retry may still succeed and
then there is nothing to announce. With no attempts left the row is never
claimed again, so this notice cannot accumulate either.

It names the **category** and nothing the tool printed:

| Failure | The notice says |
| --- | --- |
| the engine failed, with an API status | `The review engine failed (API error 404).` |
| the engine failed, without one | `The review engine failed.` |
| `engine.timeout_seconds` killed it | `The review engine ran out of time.` |
| the subprocess never started | `The review engine could not be started on the agent's host.` |

The tool's own message stays in the operator's journal. It can carry host
paths, account and quota state or whatever the CLI chose to echo, and this
comment goes under the agent's account on what may be a public repository.
The API status is quoted because it is an integer, not text.

Only engine failures are announced. A GitHub or git failure says nothing a
contributor can act on, and the post announcing it would likely fail the same
way. A usage limit spends no attempt, so it is never the last one; the
[breaker](BUDGET.md) owns it. Like the refusal notice, it is suppressed by a
dry run, never raises, and goes through `neutralise`.

## 🔁 The head is re-read immediately before posting

A review describes one commit. By the time it finishes the pull request may
have moved on, and a review of a superseded commit landing late is worse
than no review at all.

So the publisher re-reads `GET /pulls/{n}` and compares the live head with
the one the review ran against. On a mismatch the queue row is **completed**,
not retried: a push to an existing pull request is not a trigger
([TRIGGERS.md](TRIGGERS.md)), so another attempt would re-read the same stale
sha and reserve allowance to do it. What gets posted is
[below](#-a-head-that-moved-under-the-review).

[QUEUE.md](QUEUE.md#-what-the-queue-does-not-do) assigns this check here
deliberately. It has to be a live read taken as late as possible; at claim
time it would be worth nothing.

The read happens in a dry run too. An operator watching one needs to see the
same decision the real path would take, not a shortcut past it.

## 🪧 A head that moved under the review

The review is posted anyway, under a line saying what it describes:

> _This review describes `aaaaaaa`, which is no longer the head: the branch
> has since moved to `bbbbbbb`. Findings may already be addressed. Mention me
> again for a review of the new head._

This used to be a discard, and the argument for changing it is that the
tokens are **already spent** by the time the head is re-read — the engine ran
before the publisher was called, and `settle` has already charged the ledger.
Discarding saves nothing; it only guarantees nobody sees what was paid for.
Most of a review survives a fixup commit. The one thing a reader genuinely
needs — which commit the words describe — is what the note gives them. Since
1.3.0 the note is not replaced by the next round: each review posts
[its own comment](#-one-comment-per-review), so a reader scrolling the thread
sees the review of the old commit, marked as such, above the review of the
new one. That is the honest record of what was reviewed and when.

`publish.post_superseded: false` restores the discard for an operator who
would rather have silence.

**Either way the run is stamped.** That half is not configurable, and it is
[issue #68](https://github.com/prasadtalasila/pr-review-agent/issues/68): the
discard path used to return before `_stamp`, leaving `published_at` NULL, so
the run stayed "still owed a comment" for the lifetime of the database. The
`publish_outcome` column added in migration 12 is what lets a stamped row say
*which* ending it had, which `published_at` alone cannot.

## 💬 One comment per review

Findings are posted as an ordinary issue comment on the pull request's
conversation, and a re-review **posts its own comment**. Nothing the agent
has posted is ever edited, and the `comment_id` recorded against a run is
never dereferenced again.

**This reverses the original design, and the reason is worth keeping.** Until
1.3.0 the agent kept one comment per pull request and rewrote it on every
round: what a reader saw was the agent's current opinion rather than a thread
of superseded machine opinion to scroll past. That reads well, and it made
the last step of a paid review depend on a comment anybody could delete. A
maintainer tidying a thread — "resolved, let's clean this up" — left an id
that answered `404` forever. The `PATCH` raised, the worker handed the row
back *unattempted*, the next claim did exactly the same thing, and a review
the account had already been billed for was never seen by anyone while an
`ERROR` appeared in the log every idle period. That is
[issue #71](https://github.com/prasadtalasila/pr-review-agent/issues/71).

A 404 fallback would have patched that one path. Posting afresh removes the
class: a write that creates something cannot fail because somebody deleted
something else. What is gained beyond the fix is that each reviewed commit
keeps a durable, linkable comment — a maintainer can quote round 2 in a
discussion and the quote still means what it meant. What is given up is the
tidy thread, and two things bound the untidiness:

- [`pacing`](BUDGET.md#-the-pacer) collapses a burst of triggers on one pull
  request into one review, so an active branch does not produce a comment per
  push.
- `ReviewQueue.fold` closes every trigger already waiting when a review
  starts, so three maintainers mentioning the agent get one comment, not
  three.

The body names the commit it describes, because a pull request under review
has several and a reader has to be able to tell which revision each comment
is about. It also names the round and the commit count — "round 3" and
"round 1" are different statements, including when both found nothing. The
commit count comes off the live pull request payload the publisher already
reads to decide whether the head moved, so it costs no extra round trip and
no database column.

Findings still render in a pinned section-then-number-then-location order.
That used to be what made an edit a no-op diff; it now serves the reader
comparing this round's comment with the last one, for whom only a stable
order makes the things that changed the things that stand out.

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
A comment the agent posts is a comment the poller reads back on the next
cycle.

Left alone that is a loop: the agent posts, the classifier accepts what it
posted, and the agent reviews the pull request again. Two things stop it, and
this module holds both ends.

**The id is recorded.** Every comment posted here is written to
`agent_comments`, and the classifier drops a comment whose id it finds there
as `self_comment` — see
[TRIGGERS.md](TRIGGERS.md#-the-agent-cannot-summon-itself). That is the
structural half. It replaces the bound that came for free while the agent
rewrote one comment per pull request: the id never changed, so the dedupe key
capped a runaway loop at one extra paid review. A comment per review mints a
new id every round ([issue #108](https://github.com/prasadtalasila/pr-review-agent/issues/108)),
and the recorded set is stricter than what it replaces — no extra review at
all. The write happens **before** the run is stamped: a crash between the two
leaves a comment the agent will not answer, where the other order leaves one
it might.

**The text is neutralised.** Below, and unchanged. The set is database state
and the escape is in the posted bytes, so neither subsumes the other.

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

**It is counted on the run instead.** `publish.max_publish_attempts` (default
10) bounds how many posts one recorded review may cost before the agent gives
up on it: `runs.publish_attempts` counts them, `runs.publish_failed_at` stamps
the last one, and a stamped run is never offered for publication again. Ten
rather than the queue's three, because the findings are already paid for — the
cost of one more attempt is one HTTP request, and the cost of stopping too
early is a review nobody sees. What this bounds is the case no retry can fix:
a locked pull request, a repository whose issues were turned off, a token that
lost its scope. The `ERROR` names the run, and an operator who fixes the cause
puts it back in the queue with:

```sql
UPDATE runs SET publish_failed_at = NULL, publish_attempts = 0
 WHERE dedupe_key = '<key from the ERROR>';
```

| Publish outcome | Queue verb | Why |
| :-- | :-- | :-- |
| published | `complete` | Done. |
| dry run | `complete` | The pipeline ran; there is nothing to retry. |
| superseded | `complete` | The head will never match again; the review is posted saying so unless `post_superseded` is off. |
| write failed, engine ran | `complete`, plus a `publish` item | The review is done; only the posting is outstanding. |
| write failed, `publish` item | `release_unattempted` | Retryable, and the attempt does not count against `max_attempts`: nothing was spent. |
| `max_publish_attempts` failed posts | `complete`, and the run is stamped | No retry will fix it; an `ERROR` names what an operator has to look at. |
| run already posted or purged | `complete` | Nothing left to post, so the item is finished. |

A publish-only retry therefore never exhausts `max_attempts`. That is
deliberate — the bound measures allowance drained, and this drains none — and
until 1.3.0 it meant a comment GitHub will *never* accept was retried for as
long as the item stayed claimable, which `STATUS.md` recorded as a known gap.
`max_publish_attempts` closes it without touching what `max_attempts` means.

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
on every claim for the lifetime of the database. So does a superseded run,
and for the same reason — see [above](#-a-head-that-moved-under-the-review).

## 📋 The `runs` table

See [STORAGE.md](STORAGE.md#-schema) for the columns. Four decisions worth
knowing:

- **Keyed on `dedupe_key`**, matching `queue` and `ledger`, which is what
  turns "every posted comment is traceable to a ledger row recording engine,
  model, mode, usage and `usage_confidence`" into a query rather than a
  convention.
- **`comment_id` is written per run and never read back.** Since each review
  posts its own comment it is a record of what this run produced — traceable,
  and what a future line-anchored comment would need — rather than an address
  the next round writes to. Nothing dereferences it, which is why a comment a
  maintainer deletes can no longer strand a paid review.
- **`publish_attempts` and `publish_failed_at` bound the publish-only retry,**
  which `max_attempts` deliberately does not measure. See
  [above](#-a-paid-review-is-kept-until-it-can-be-posted).
- **`findings` is JSON text, not a child table.** Written once, read once,
  purged wholesale; nothing queries a finding by path, line or severity.
- **`publish_outcome` says how publishing ended,** which `published_at`
  cannot: a posted review and one discarded because the head moved both have
  to be stamped, and only the outcome column tells them apart afterwards.
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
