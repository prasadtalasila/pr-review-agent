# Triggers

What starts a review, what does not, and why each rejection is the shape it
is. Implemented in `src/pr_review_agent/triggers/`, tested in
`tests/test_classifier.py`, `tests/test_allowlist.py` and
`tests/test_mention.py`.

## ✅ The two accepted events

1. A **freshly opened pull request** whose **author** is allowlisted.
2. A **comment containing `@claude`** whose **commenter** is allowlisted.

Gating the mention on the commenter rather than the pull request author is
what lets a maintainer summon a review of an outside contribution that would
not be auto-reviewed. It is the whole answer to "restricted eligibility, but
outside contributions still get reviewed when we want them to".

## 🔀 The decision flow

```text
                     polled pull request or comment
                                  │
                                  ▼
          at or below the watermark? ──yes──► not_fresh
                                  │no
                  ┌───────────────┴────────────────┐
             pull request                       comment
                  │                                  │
                  │                   on an open pull request?
                  │                   ──no──► pr_not_open
                  │                                  │yes
                  ▼                                  ▼
                  a bot account? ──yes──► bot_author / bot_commenter
                  │no                                 │no
                  ▼                                   ▼
          a draft? ──yes──► draft      mentions @handle outside a
                  │no                  fence, code span or blockquote?
                  │                    ──no──► no_mention
                  └───────────────┬────────────────┘
                                  ▼
                actor's numeric id allowlisted? ──no──► *_not_allowlisted
                                  │yes
                                  ▼
                            Trigger ──► enqueue
```

Freshness comes first because anything at or below the watermark has already
been decided, whoever wrote it — so no other reason is informative, and a
a re-seen item is reported as `not_fresh` rather than repeating
`bot_commenter` on every cycle. No accept/reject outcome depends on the
order: every one of these paths rejects either way.

Every arrow in that diagram is one row of the table below, with the reason
code and log level it is rejected at.

## 🚫 Every rejection, and its reason code

Every row below is logged at `DEBUG`, accepted decisions included.

| Event | Reason code |
| :-- | :-- |
| Push to an existing pull request | *never classified* — the poller emits no push event |
| Draft pull request | `draft` |
| Pull request from an unlisted author | `author_not_allowlisted` |
| Comment from an unlisted account | `commenter_not_allowlisted` |
| Any bot account | `bot_author` / `bot_commenter` |
| `@claude` in a fence, code span or blockquote | `no_mention` |
| Already-open pull request seen below the watermark | `not_fresh` |
| Comment last updated at or below the watermark | `not_fresh` |
| Comment on a pull request that is not open | `pr_not_open` |

They are reached with `--log-level DEBUG`,
`PR_REVIEW_AGENT_LOG_LEVEL=DEBUG` or `logging.level` in `config.yaml` — see
[LOGGING.md](LOGGING.md) and [CONFIG.md](CONFIG.md#logging). In JSON mode the
reason is a field rather than text, so the question can be asked of the whole
log at once:

```bash
jq -r 'select(.reason) | [.pr, .reason] | @tsv' agent.jsonl
```

Every decision is logged, accepted or not — it is the only observability the
daemon has into *why wasn't this reviewed*. One level for all of them, rather
than the per-reason split this table used to carry, because a decision fires
for every pull request and every comment on every cycle: `not_fresh` once per
already-open pull request on the first poll, `no_mention` once per comment on
every poll that returns a `200`, and `pr_not_open` once per comment on every
pull request the repository has ever closed. That is the whole record's
shape, not three exceptional reasons inside it, so the whole record sits on
the level an operator turns on to ask the question. What a review actually
did is [LOGGING.md](LOGGING.md)'s events 3 to 6, and those are louder than
the default.

### Comments are filtered to open pull requests

`/pulls?state=open` is filtered by state. The two comment endpoints are
repo-wide and are not: they return comments on pull requests closed weeks ago,
and in the v0.12.0 live run that was a hundred decisions per cycle that
nothing could ever come of — at the time, all of them visible at the default
level.

So the daemon keeps the set of pull request numbers the `/pulls` leg reported
and rejects a comment outside it as `pr_not_open`. The set lives across cycles
because that leg answers `304` whenever nothing changed, and a `304` means
*unchanged*, not *unknown*. Until the first `200` the set is unknown and the
filter is off — failing open costs a few `DEBUG` lines, whereas failing closed
would silently drop every mention.

Both legs belong to one sweep and the pulls leg is classified first, so a
comment on a pull request opened in that very cycle is still matched. The
trade-off worth stating: **a mention posted on a pull request that closes
before the next cycle is dropped.** That is a behaviour change rather than
only a quieter log, and it is the intended reading — the agent has nothing
useful to say about a closed pull request.

### Drafts and mentions

The draft check applies to **fresh pull requests only**. `draft` exists to stop
the agent auto-reviewing work in progress nobody asked about; an allowlisted
human typing `@claude` on a draft *is* the ask, and refusing it would make the
handle unreliable exactly when a contributor wants early feedback.

## 🔁 The agent cannot summon itself

There is **no check on which account the agent posts as**. There used to be:
`self_author` and `self_commenter` rejected any event whose actor id matched
`github.agent_user_id`, and that key is gone with them.

The loop they guarded is real. A review body is engine prose over the tree
being reviewed, and when that tree is *this* repository the prose names
`@claude` readily. The comment is also edited in place on re-review, which
bumps `updated_at`, so it comes back to the poller as fresh. Left alone, the
agent answers itself — bounded by the dedupe key to one extra paid review per
pull request, which is one more than nobody asked for.

It is closed in the publisher instead. `publisher.render` runs every body it
posts through `mention.neutralise`, which rewrites exactly the mentions
`has_mention` would find — and only those, so a handle inside a code span is
left as the reader wrote it — into `&#64;`. GitHub renders the entity as `@`,
so a reader sees no difference; the raw body a later poll reads back has no
`@` for the detector to match.

That is the stronger place for the check, and issue #36 is why. An identity
check rejects an **account**: on a deployment where one account is both the
reviewer and the reviewed — a single-maintainer repository, which is what
`config.example.yaml` describes — it rejected every event from the only human
who used the agent, before the allowlist was ever consulted. Neutralising the
output rejects the **text**, which is what the loop was ever made of.

This is also how [pr-agent](https://github.com/qodo-ai/pr-agent) closes the
same loop: its trigger is a `/review` command at the head of a comment, and
its own output is prose under a heading, so it cannot match. It needs no
notion of its own identity either. The difference is only that `@claude` is
an ordinary word in prose where `/review` is not, so the exclusion has to be
made rather than inherited.

**What it widens.** Events from the account the agent posts from are no
longer rejected on identity. The allowlist is now the only thing gating that
account — and it already was, for every other account.
`tests/test_classifier.py::test_the_account_the_agent_posts_from_can_still_trigger_a_review`
and `::test_the_allowlist_is_the_only_thing_that_was_loosened` pin both
halves of that bound.

## 🆔 Allowlisting is on the numeric user id

Membership is decided on the numeric GitHub user id, **never** the login.

A login can be renamed and the freed name registered by somebody else, which
would silently transfer eligibility to a stranger. Keying on the id closes
that; `tests/test_allowlist.py::test_rejects_impostor_who_took_the_freed_login`
pins it.

A login in the config raises `AllowlistConfigError` at startup rather than
never matching, because a login-keyed allowlist does not fail loudly — it
silently disables every trigger. So does a non-positive id, and so does a
value like `"--5"` that only *looks* numeric. Parsing and validating happen in
one step (`_coerce_user_id`) precisely so that a shape check and a later
`int()` cannot disagree.

Any new trust check must follow the same rule: identity is a number.

## 💬 What counts as a mention

`@claude` counts only when it is something the commenter **wrote as an
instruction**. Before the search, `strip_non_prose` blanks:

- fenced code blocks (3+ backticks or tildes, indentable by up to 3 spaces; an
  unclosed fence runs to the end of the document, per CommonMark);
- indented code blocks (4+ spaces or a tab);
- inline code spans, bounded by a matching run of backticks;
- blockquotes.

Line structure is preserved so reported positions stay meaningful.

Without this, a diff containing `@claude` in a code sample, or a reply quoting
an earlier mention, would re-trigger a review — an expensive kind of wrong.

The pattern is word-bounded in both directions: the lookbehind rejects an
address such as `user@claude.ai`, and the lookahead rejects a different account
such as `@claude-ci`.

The handle is configurable (`triggers.handle`), which is what makes the
pipeline reusable for a different agent.

## 🔑 Dedupe keys

```text
pr_opened:{repo}:{pr}:{head_sha}
mention:{repo}:{pr}:{comment_id}
```

The two namespaces never collide, so a pull request and a comment on it cannot
deduplicate against each other.

The mention key excludes `head_sha` **on purpose**: the same comment must never
trigger twice, and a subsequent push must not revive it. The pull-request key
includes it for the opposite reason — a new head is genuinely new work, and the
dedupe key is what stops an unchanged head being reviewed twice.

## ⏳ The cold-start watermark

`Classifier.since` is load-bearing. The poller sees *open* pull requests, not
`opened` webhook events, so without a watermark the first poll would treat the
entire open backlog as fresh and review all of it at once — burning the weekly
allowance in a single pass.

Two properties follow:

- **It must be timezone-aware.** GitHub timestamps are aware UTC (`...Z`), and
  comparing one against a naive `since` raises `TypeError` on the first pull
  request seen. `Classifier.__post_init__` rejects a naive watermark at
  construction instead.
- **It must survive a restart, and only move forward.** See
  [STORAGE.md](STORAGE.md).

### Comments are watermarked too

The same argument applies to mentions, and the cost of getting it wrong is
higher: a pull request below the watermark is merely re-offered, whereas every
historical `@claude` in the newest hundred comments would be replayed as a
fresh request. A comment is therefore compared against the `comments`
watermark on `updated_at`, and rejected `not_fresh` at or below it.

Two consequences are deliberate:

- **An edit summons a review.** Editing `@claude` into an existing comment
  bumps `updated_at`, so the comment becomes fresh and is accepted. That is
  the right reading of an allowlisted maintainer's intent.
- **A re-review is stopped by the dedupe key, not by the watermark.** An
  edited comment that was *already* a mention carries the same
  `mention:<repo>:<pr>:<comment_id>` key, so re-classifying it enqueues
  nothing.

Both comment endpoints share the single `comments` watermark. GitHub's
`updated` only ever moves forward, so one high-water mark cannot hide a
comment that surfaces later on the other endpoint.

## 👻 Unmappable events

An event nobody is accountable for cannot be allowlisted. A deleted (ghost)
account arrives as `"user": null`, so `Actor.from_api` raises a typed
`PayloadError` and the payload layer skips that one item with a warning rather
than failing the cycle. One malformed item must not stop the other ninety-nine
from being classified.
