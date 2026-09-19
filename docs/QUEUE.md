# Queue and lease

Where an accepted trigger waits, and what stops the same review being paid
for twice. Implemented in `src/pr_review_agent/queue.py`, over the `queue`
table declared in [STORAGE.md](STORAGE.md#-schema).

## 🧷 Why a queue at all

A trigger is not reviewed where it is classified. It is enqueued, and a worker
claims it later.

That indirection is what the spending rails need. A claim is the single point
at which work becomes expensive, so it is the single point the [budget
governor](BUDGET.md) has to guard — and the reservation is taken inside the
*same* transaction as the claim, through the `admit` hook below.

## 🔑 A trigger is enqueued at most once

The [dedupe key](TRIGGERS.md#-dedupe-keys) is the table's primary key, so
`enqueue` is an `INSERT OR IGNORE` that returns whether the row was new. A
re-poll that sees the same freshly opened pull request, or the same `@claude`
comment, inserts nothing.

**Rows are kept after completion.** That is the point: the key is what makes
"already reviewed" a fact rather than a guess. A pull request whose head moves
gets a new key and is genuinely new work; a mention never does, because its key
carries the comment id instead of the head.

## 🔒 One pull request, one worker

A claim is refused while any *other* row for the same `(repo, pr_number)`
holds a live lease. A maintainer's `@claude` arriving mid-review therefore
waits for the run in flight rather than racing it.

The claim is a read that picks a row followed by a write that leases it.
SQLite has no `SKIP LOCKED`, so the pair runs inside one `BEGIN IMMEDIATE`
transaction — the write lock is taken up front, which is what makes it atomic
against another writer.

## 🔌 The `admit` hook

`claim(now=, owner=, admit=None)` takes an optional predicate
`(connection, claim, now) -> bool`, called **inside** the claim's own
transaction. That is what makes the budget reservation atomic with the lease:
the governor writes its ledger row on the connection it is handed, and the row
and the lease commit together or not at all.

It is a plain predicate rather than anything richer, so no budget type appears
in this signature and `queue.py` imports nothing from the governor.

**A refused candidate is skipped, not final.** `_CLAIMABLE` has no `LIMIT 1`;
`claim` walks the ordered candidates and leases the first one `admit` accepts.

That matters at the ladder's 85 % rung, where fresh pull requests stop being
auto-reviewed but an explicit `@claude` is still honoured. A refused pull
request keeps its `pending` status and its attempt count — a refusal is about
the allowance, not the trigger, and burning an attempt would let three
refusals abandon a perfectly good one. So it correctly stays at the head of a
FIFO queue, and without skipping it would block the maintainer's mention
behind it until the window rolled: days.

Every other condition in `_CLAIMABLE` is a fact about the row, which SQLite can
already exclude on. A budget refusal is the first decision it cannot express,
because it depends on the ledger, the rung and the trigger's kind.

The candidates are read out with `fetchall` before one is leased, because
committing with a half-consumed cursor still open raises "SQL statements in
progress". The claimable set is bounded by the pending queue, which is small.

**The hook is optional, and that is the known weakness of the seam.** Nothing
in the type system stops a caller claiming without a governor;
[DESIGN.md](DESIGN.md#-the-one-rule)'s one rule, review, and the fact that the
only claimer will be the engine phase's worker are what hold it.

## ⏱ Leases expire; they are not renewed

The lease carries an expiry and no heartbeat. A run has a wall-clock ceiling
([BUDGET.md](BUDGET.md), layer 3), so a lease set above that ceiling cannot
lapse under a worker that is still alive, and renewal would be machinery for a
case that cannot arise. The default is 30 minutes.

An expired lease makes the row claimable again — that is the crashed-worker
path — and `release` hands work back immediately without waiting the lease out.

`complete` and `release` are both **guarded on the owner**. A worker whose
lease lapsed and was re-claimed by somebody else gets `False` rather than
finishing the newer worker's row, which is how it learns to discard its result
instead of publishing it late.

## 🧯 Retries are bounded

Each claim increments `attempts`, and a row that has used up `max_attempts`
(default 3) is marked `abandoned` instead of being offered again.

Without that bound, a trigger that crashes its worker every time would be
re-reviewed forever, and **each attempt spends allowance before it fails**.
Three is enough to ride out a transient failure and few enough that a poison
trigger cannot drain the weekly allowance one retry at a time.
`tests/test_queue.py::test_retries_are_bounded` pins it.

## 🧬 Statuses

```text
enqueue                     claim()                complete()
(INSERT OR IGNORE)  ──►  pending  ──────────────►  claimed  ──────────────►  done
                      (attempts+1)
```

`claimed` returns to `pending` on `release()` or when the lease simply
lapses; either way `attempts` is already counted. `abandoned` is reached from
either state: `claim()` reaches it directly when a row's `attempts` are
already at `max_attempts`, and a worker holding `claimed` reaches it by
calling `abandon()` on a failure that will only fail again.

| Status | Meaning |
| :-- | :-- |
| `pending` | Waiting for a worker. |
| `claimed` | Leased until `leased_until`, by `owner`. |
| `done` | Reviewed. Never offered again. |
| `abandoned` | Given up on. Never offered again. |

`abandoned` is reached two ways: by using up `max_attempts`, which `claim`
does for itself, and by a worker calling `abandon` on a failure that will
fail again — an oversized pull request, an unusable payload. See
[WORKER.md](WORKER.md#-what-happens-to-the-row).

`complete`, `release` and `abandon` are the three verbs a worker closes a row
with, and all three are guarded on the owner.

## 🔭 What the queue does *not* do

`Claim.trigger.head_sha` is the head observed when the trigger was
*classified*, and for a mention it is `None` — the comment payload does not
carry one ([POLLER.md](POLLER.md#-mapping-a-payload-to-a-pull-request)).

It is what the review runs against, but it is **not** what makes the review
safe to post. The [publisher](PUBLISHER.md#-the-head-is-re-read-immediately-before-posting)
re-reads the live head immediately before posting and discards a review of a
superseded commit. The queue cannot do that check itself: it would have to be
a GitHub read, and it has to happen at publish time rather than claim time to
be worth anything.

The queue also does not know that a run has been *paid for but not posted*.
That lives in `runs`. The worker's `admit` predicate consults it, and admits
such a claim without reserving anything — publishing reaches no engine, so
weighing it against a budget window would be refusing to spend nothing. Such
a claim is also handed back with `release_unattempted` if the post fails,
because `max_attempts` bounds allowance drained and this drains none. See
[PUBLISHER.md](PUBLISHER.md#-a-paid-review-is-kept-until-it-can-be-posted).
