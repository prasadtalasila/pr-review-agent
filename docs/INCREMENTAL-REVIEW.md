# Incremental review

A later round on a pull request is shown only what changed since the head the
previous completed round reviewed, not `merge_base..head` again. This page is
the whole feature in one place. The mechanism lives in
[WORKSPACE.md](WORKSPACE.md#-a-later-round-sees-only-what-changed), the
spending rules in [BUDGET.md](BUDGET.md), and the two settings in
[CONFIG.md](CONFIG.md#budget). Shipped in 1.9.0 (#124). The coverage footer
followed in 1.10.0 (#123).

## 🔁 What a round is shown

| Round | Diff the engine reads |
| :-- | :-- |
| First on the pull request | `merge_base..head`, the whole pull request |
| After a completed round | Only what changed since that round's head |

Trees are compared, not history, so a force-push is the ordinary case rather
than a reason to start over. A fixup shows the fixup. An amend, squash,
reword or reorder shows what the rewrite changed, which is often nothing. A
rebase or a merge from the base shows only what changed beyond it, because
the old head is first replayed onto the new merge base with
`git merge-tree --write-tree`.

Some rounds fall back to a full review, and the log says why: the first
round, an unmoved head, an old head the mirror no longer holds, a replay that
conflicts, a git older than 2.38 (for rebases only), or a round below either
threshold. The [full list](WORKSPACE.md#-a-later-round-sees-only-what-changed)
is in WORKSPACE.md.

What does not narrow:

- **The tree on disk** is always the whole head.
- **The previous round's findings** reach the prompt in full. The prompt says
  the diff is incremental and that an earlier finding may sit outside it, so
  "still present" can still be checked.

## ⚙️ Settings

```yaml
budget:
  incremental_min_commits: 0   # below this many new commits, review in full
  incremental_min_seconds: 0   # previous round newer than this: review in full
```

Both default to `0`, which disables them. At or below
`min_review_interval_seconds`, `incremental_min_seconds` never fires, because
the pacer defers such a trigger first. See [CONFIG.md](CONFIG.md#budget).

## 💰 What it changes about spending

- **The size caps measure the narrower range.** A pull request whose whole
  diff is over `max_changed_lines` is reviewed when the commits since the last
  round fit under it. A fixup that is itself over the cap is still refused.
  One variable feeds both the `--numstat` count and the diff, so the caps
  bound exactly what the engine is shown. See
  [BUDGET.md](BUDGET.md#the-size-gate-moved-to-make-this-possible).
- **Only full rounds train the pre-flight estimate.** The ledger records the
  head an incremental round diffed from in `reviewed_since`, and the fit reads
  only rows where that is NULL. See
  [BUDGET.md](BUDGET.md#-the-pre-flight-token-estimate).
- **An empty incremental range is refused for free.** This is usually a
  force-push that changed no content, and the notice says nothing changed
  since the previous head.

## 🧾 What the posted review says

The [coverage footer](reporting/review-report.md#rules) names the changed
files `budget.excluded_paths` kept from the engine. On an incremental round
it covers the same range as the diff, because `Checkout.omitted` is computed
from the same `start..head` span. A lockfile changed only in an earlier round
is therefore not listed again.

The report does not yet say that the round itself was incremental, or which
head it diffed from. `Checkout.since_sha` already carries what that would
need.
