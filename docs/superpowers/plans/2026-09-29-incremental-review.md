# Incremental Review: Diff Since the Last Reviewed Head — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A later round on a pull request is shown only what changed since
the head the previous completed round reviewed, and is size-gated and
cost-estimated on that narrower range. Force-pushes are common on the target
repositories, so an amend, a squash or a rebase must be incremental too, not
a reason to fall back.

**Architecture:** The range is a git question, so it is answered in a new
`workspace/since.py`, called from `Workspace.checkout`, which records the
answer on `Checkout.since_sha`. `RunStore.history` hands the worker the
previous round's head and time. The prompt header names the range. The
ledger gains `reviewed_since`, and the pre-flight fit reads only full rounds.
Two budget keys, `incremental_min_commits` and `incremental_min_seconds`,
default to `0`, meaning disabled.

**Spec:** https://github.com/prasadtalasila/pr-review-agent/issues/124
(roadmap C2, sub-issue of #120). Shipped in 1.9.0.

---

## The design: compare trees, not history

The first draft of this plan used `git merge-base --is-ancestor` and fell
back to a full round on any force-push. That matches pr-agent, and on a
project where force-pushes are the norm it saves tokens on a minority of
rounds. It was replaced before implementation.

The reviewer never read the commits; it read the old head's content. So:

1. **Same merge base** (fixup, amend, squash, reword, reorder):
   `git diff <old head> <new head>`. No ancestry needed. A content-identical
   force-push diffs to nothing.
2. **Merge base moved** (rebase onto a newer base, or a merge from the
   base): replay the old head's changes onto the new merge base with
   `git merge-tree --write-tree <new merge base> <old head>`, and diff from
   that tree. What remains is exactly what changed beyond the rebase.
   A line equal in the replay and the new head is either reviewed content or
   upstream content, so nothing the contributor wrote is hidden.
3. **Full round, logged with the reason:** first round; head unmoved; old
   head not in the mirror; replay conflicts (exit 1); git older than 2.38
   (no `--write-tree`; warned once, and fixups and amends stay incremental);
   below either threshold.

The git floor stays 2.32. Nothing is refused because of the version.

**CLAUDE.md §5 disclosure.** The size caps now measure the incremental
range, so a pull request whose whole diff exceeds `max_changed_lines` is
reviewed when the commits since the last round fit. The caps still bound
every range the engine is shown, because one variable feeds both the
`--numstat` and the diff. `test_a_small_fixup_on_an_oversized_pull_request_is_reviewed`
and `test_a_fixup_larger_than_the_cap_is_still_refused` pin the bound.

## Tasks

- [x] **History.** `_HISTORY` selects `head_sha, recorded_at`;
      `PullRequestHistory.incremental_base(now, min_seconds)`.
      Tests in `tests/test_runs.py`.
- [x] **Config.** `incremental_min_commits`, `incremental_min_seconds`;
      both example configs; `docs/CONFIG.md`.
      Tests in `tests/test_config_budget.py`.
- [x] **Workspace.** `workspace/since.py::diff_start`; `Checkout.since_sha`;
      `checkout(since_sha=, min_commits=)`. A `Contributor` fixture in
      `tests/conftest.py` makes real pushes against the loopback remote.
      Tests in `tests/test_workspace_since.py`: fixup, amend, reword, rebase,
      merge from base, exclusions, both §5 bounds, first round, unmoved head,
      unknown head, conflicting replay, pre-2.38 git, commit threshold.
- [x] **Prompt.** `_range` states the whole range or "only what changed
      since", and that earlier findings may be outside the diff.
      Tests in `tests/test_cli_engine_prompt.py`.
- [x] **Ledger and fit.** Migration 16 adds `ledger.reviewed_since`;
      `_FIT_SAMPLE` excludes non-NULL rows; `settle(reviewed_since=)`.
      An empty incremental range gets its own refusal notice.
      Tests in `tests/test_budget_fit.py`, `tests/test_store_*.py`.
- [x] **Worker.** Passes the range in, the prior findings unnarrowed, and
      `reviewed_since` to settle. Tests in `tests/test_worker_review.py`.
- [x] **Docs.** `docs/WORKSPACE.md`, `docs/BUDGET.md`,
      `docs/FEATURE-ROADMAP.md`, both package trees.
- [x] **Release.** 1.9.0: new config keys and a schema migration.

## Decisions taken on the open questions

1. `incremental_min_seconds` is implemented as the issue states, default
   off, with the pacer interaction documented: at or below
   `min_review_interval_seconds` it never fires.
2. An empty incremental range is refused for free, with a notice saying
   nothing reviewable changed since the previous head.
3. The posted report does not say the round was incremental. That belongs
   with roadmap Q4, the coverage footer.

## Known limit

`runs.head_sha` is the API's head from the facts read, not the fetched one.
The fetch happens after the read, so the recorded head can only be older
than what was reviewed, which makes the next incremental diff wider, never
narrower.
