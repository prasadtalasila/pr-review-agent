# Queue and lease

Where an accepted trigger waits, and what stops the same review being paid
for twice. Implemented in `src/pr_review_agent/queue.py`, over the `queue`
table declared in [STORAGE.md](STORAGE.md#-schema).

## 🧷 Why a queue at all

A trigger is not reviewed where it is classified. It is enqueued, and a worker
claims it later.

That indirection is what the spending rails need. A claim is the single point
at which work becomes expensive, so it is the single point the [budget
governor](BUDGET.md) has to guard — and the reservation is specified to be
taken inside the *same* transaction as the claim.

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

| Status | Meaning |
| :-- | :-- |
| `pending` | Waiting for a worker. |
| `claimed` | Leased until `leased_until`, by `owner`. |
| `done` | Reviewed. Never offered again. |
| `abandoned` | Used up `max_attempts`. Never offered again. |

## 🔭 What the queue does *not* do

`Claim.trigger.head_sha` is the head observed when the trigger was
*classified*, and for a mention it is `None` — the comment payload does not
carry one ([POLLER.md](POLLER.md#-mapping-a-payload-to-a-pull-request)).

It is what the review runs against, but it is **not** what makes the review
safe to post. The publisher re-reads the live head immediately before posting
and discards a review of a superseded commit. The queue cannot do that check
itself: it would have to be a GitHub read, and it has to happen at publish
time rather than claim time to be worth anything.
