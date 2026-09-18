# Publisher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a computed review visible. Acknowledge a claimed trigger with 👀 immediately, re-check the head against the live one, and post exactly one comment per pull request — edited in place on re-review — with no code path able to approve, request changes or merge.

**Architecture:** A `Publisher` over write verbs added to the existing `GitHubClient`, plus a `runs` table that persists what a paid review produced so a failed *publish* is retried without a second review. The worker gains two call sites — an acknowledgement right after the claim, and a publish between `settle` and `complete` — and one new entry path: a claim whose run is already recorded and unpublished skips the engine entirely.

**Tech Stack:** Python 3.10–3.14, asyncio, `httpx`, SQLite via `store.py`, pytest with `asyncio_mode = "auto"` and `httpx.MockTransport`.

**Issue:** https://github.com/prasadtalasila/pr-review-agent/issues/27

## Decisions taken before planning

These were open questions in the issue; they are settled here and the tasks below assume them.

1. **Findings are posted as one plain issue comment on the pull request's conversation**, via `POST /repos/{o}/{r}/issues/{n}/comments` and `PATCH /repos/{o}/{r}/issues/comments/{id}`. The publisher never calls `POST /pulls/{n}/reviews`. The anti-approval guarantee in `DESIGN.md` §Prompt injection (3) therefore rests on an **absent capability** rather than a guarded field — the strongest form available, and the one the issue asks for when it says "no code path that can emit another event".
2. **The 👀 goes on the triggering comment**, not on the pull request, so it lands where the person actually typed `@claude`. This requires carrying `comment_id` and the comment's source endpoint through the payload mapping, the `Trigger` and the `queue` table. A `pr_opened` trigger has no comment, so it reacts on the pull request itself.
3. **The `runs` table lands in this branch**, and a publish that fails after a paid review is retried publish-only.
4. **Edit-in-place applies to every review, not only a clean one.** The issue constrains only the clean case, but one agent comment per pull request, rewritten on re-review, is a single code path where two would be needed otherwise — `CLAUDE.md` §2. The consequence is explicit: the *text* of a superseded review is replaced, and its history lives in `runs` and the ledger rather than in the comment thread.

## Assumptions, stated rather than silently taken

- **"Within 15 s of the trigger" is measured from the claim, not from the GitHub event.** The poll interval is adaptive 10–600 s (`POLLER.md`), so no acknowledgement can be within 15 s of a comment being written. The issue itself says the reaction is what "makes the polling latency imperceptible", which only parses if the 15 s is the gap between the agent seeing the trigger and responding to it. `ROADMAP.md`'s acceptance criterion is reworded accordingly in Task 9.
- **The issue says "schema migration 5". It is now 6 and 7** — migration 5 is `ALTER TABLE ledger ADD COLUMN reviewed_lines`, added by #25 after the issue was written.
- **A budget refusal defers publication of an already-paid review.** The publish-only retry goes through `queue.claim`, which calls `governor.admit`; if every window is exhausted the row stays pending and publishes when a window rolls. The alternative — a publish path outside the lease — means a second lease implementation and a duplicate-comment race between workers, which is not worth it for a failure mode that resolves itself. Noted in `ROADMAP.md` §Known gaps.
- **`ROADMAP.md` and the ARCHITECTURE diagram say "line-anchored review".** Decision 1 makes that inaccurate. Task 9 rewords it rather than leaving a criterion the code does not meet.
- **`docs/ARCHITECTURE.md:63` is already stale** — "Nor does any engine that could spend" has been untrue since #26. Fixed in Task 9 because that paragraph is being rewritten anyway; it is the one change in this branch that does not trace to the issue.

## Global Constraints

- **Nothing may call a review engine outside the budget governor.** This branch adds no engine call site. The publish-only resume path settles at zero `exact` tokens immediately after claiming and never reaches an engine; a test pins that. (`CLAUDE.md` §5)
- **This branch widens nothing about spending.** No cap is removed, no trigger is added. The `runs` table and the publisher cost GitHub requests, not tokens.
- **Allowlisting stays on the numeric GitHub user id.** No new trust check is introduced. `github.agent_user_id` is already required (#24) and is what keeps a posted comment from re-triggering a review; a test pins that the publisher's own comment is classified `self_commenter`.
- **Comment bodies, PR bodies and findings are untrusted input.** A finding's `body` is rendered into a comment the agent posts under its own account: it must not be able to widen what the agent does. Markdown is not sanitised (GitHub renders it in a sandbox), but the publisher must never interpret a finding as a directive, and the absent-capability property in Decision 1 is what makes that structural.
- **`publish.dry_run` is an operator brake, like `budget.enabled`.** It reloads on `SIGHUP` through the mechanism `CONFIG.md:280` already promises, and a broken file leaves the previous value in force.
- Supported Python range is `>=3.10,<3.15`; `target-version = "py310"`. No 3.11+ syntax. `enum.StrEnum` comes from `._compat`, never from `enum`.
- Line length 88 (ruff). Ruff lint rules: `E`, `F`, `I`, `UP`, `B`, `SIM`.
- Pyright runs over **`src` and `tests`** in `basic` mode — test helpers must type-check too.
- Pylint must score ≥ 9.0 on `src` and on `tests`.
- The full local gate before claiming done: `poetry run pytest`, `poetry run ruff check .`, `poetry run ruff format --check .`, `poetry run pylint src --rcfile=.pylintrc --fail-under=9.0`, `poetry run pylint tests --rcfile=.pylintrc --fail-under=9.0 --disable=missing-function-docstring,missing-module-docstring`, `poetry run pyright src tests`. Quote the result; never predict it.
- **No test posts to GitHub.** Every publisher test drives `httpx.MockTransport`. The suite needs no network egress and spends no tokens.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
| :-- | :-- |
| `src/pr_review_agent/publisher.py` | `Publisher`: `acknowledge`, `publish`, the comment body rendering, `dry_run`, `reload` |
| `src/pr_review_agent/runs.py` | `RunStore` and `RecordedRun`: persist a paid review's findings, its comment id, and its publication state |
| `src/pr_review_agent/store.py` | Migrations 6 (`queue.comment_id`, `queue.comment_source`) and 7 (`runs`) |
| `src/pr_review_agent/poller/client.py` | `post` and `patch` — the first write verbs on the client |
| `src/pr_review_agent/poller/endpoints.py` | Reaction and issue-comment paths |
| `src/pr_review_agent/poller/payloads.py` | Carry `comment_id` and the source endpoint through the mapping |
| `src/pr_review_agent/triggers/models.py` | `CommentSource`; `Comment.source`; `Trigger.comment_id` / `Trigger.comment_source` |
| `src/pr_review_agent/triggers/classifier.py` | Populate the two new `Trigger` fields |
| `src/pr_review_agent/queue.py` | Persist and restore the two new fields |
| `src/pr_review_agent/config.py` | `PublishConfig`, `Config.publish` |
| `src/pr_review_agent/worker.py` | Acknowledge after the claim; record, re-check and publish after the settle; the resume path |
| `src/pr_review_agent/daemon.py` | Build the publisher, pass it to workers, reload `publish` on `SIGHUP` |
| `src/pr_review_agent/bootstrap.py` | Check the token has write scope |
| `tests/test_publisher.py` | Rendering, edit-in-place, head re-check, dry run, the absent-capability guarantee |
| `tests/test_runs.py` | `RunStore` round-trip, publication state, purge |
| `tests/test_worker.py` | The two new call sites and the resume path |

`publisher.py` imports `poller.client`, `runs`, `engine.models` and `triggers.models`. Nothing imports `publisher.py` except `worker.py` and `daemon.py`; `runs.py` imports only `store.py` and `engine.models`, in the same direction `queue.py` imports `store.py`.

---

### Task 1: The `publish` section

**Files:**
- Modify: `src/pr_review_agent/config.py`
- Modify: `config.example.yaml`, `config.minimal.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `PublishConfig(dry_run: bool = False)` and `Config.publish`. Task 5 reads it; Task 7 reloads it.

- [ ] **Step 1: Write the failing tests** — `publish` defaults to `dry_run: false` when the section is absent; `dry_run: true` parses; a non-boolean `dry_run` raises `ConfigError`; `publish` appears in the allowed top-level sections and an unknown key inside it raises.

- [ ] **Step 2: Implement** — a frozen `PublishConfig` with a `parse` classmethod, following `WorkerConfig`. The section is **optional** and its default is `False`: unlike the budget token counts, "post for real" is the only sensible default and is not a guess about an unpublished quota.

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_config.py`.

---

### Task 2: Carry the triggering comment through to the claim

**Files:**
- Modify: `src/pr_review_agent/triggers/models.py`, `src/pr_review_agent/triggers/classifier.py`
- Modify: `src/pr_review_agent/poller/payloads.py`
- Modify: `src/pr_review_agent/queue.py`, `src/pr_review_agent/store.py`
- Test: `tests/test_payloads.py`, `tests/test_classifier.py`, `tests/test_queue.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `CommentSource` (`ISSUE` / `REVIEW`), `Comment.source`, `Trigger.comment_id: int | None`, `Trigger.comment_source: CommentSource | None`, migration 6. Task 5 reacts on them.

**Why this is needed at all:** the reaction endpoints differ — `/issues/comments/{id}/reactions` and `/pulls/comments/{id}/reactions` — so knowing the id is not enough. The distinction is already computed for free in `payloads._pr_number`: a payload carrying `pull_request_url` came from `/pulls/comments`, one that did not came from `/issues/comments`. Nothing new is fetched.

- [ ] **Step 1: Write the failing tests**
  - `payloads.comments` sets `source=CommentSource.REVIEW` for a payload with `pull_request_url`, `ISSUE` otherwise.
  - `classify_comment` copies `comment_id` and `source` onto the accepted `Trigger`.
  - `classify_pull_request` leaves both `None`.
  - `enqueue` then `claim` round-trips both fields, including the `None` case.
  - A store opened at schema version 5 migrates to 6 without losing queued rows, and a pre-existing row claims back with `comment_id is None`.

- [ ] **Step 2: Implement** — migration 6 is two `ALTER TABLE queue ADD COLUMN` statements; extend `_ENQUEUE`, `_CLAIMABLE` and `_claim`. Both fields are nullable, so existing rows need no backfill.

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_payloads.py tests/test_classifier.py tests/test_queue.py tests/test_store.py`.

---

### Task 3: Write verbs on the client

**Files:**
- Modify: `src/pr_review_agent/poller/client.py`, `src/pr_review_agent/poller/endpoints.py`
- Test: `tests/test_client.py`, `tests/test_endpoints.py`

**Interfaces:**
- Produces: `GitHubClient.post(path, json) -> dict`, `GitHubClient.patch(path, json) -> dict`; `RepoEndpoints.issue_comments(n)`, `.issue_comment(id)`, `.issue_reactions(n)`, `.comment_reactions(id, source)`.

**Design note:** `get` returns a `PollResult` because conditional requests have a 304 case. A write has none, so `post`/`patch` return the decoded body and raise `GitHubClientError` on any non-2xx — a different shape for a different thing, rather than a `PollResult` with two fields that can never be meaningful.

- [ ] **Step 1: Write the failing tests**
  - `post` returns the decoded body on 201, `patch` on 200.
  - A 4xx and a 5xx both raise `GitHubClientError` naming the status.
  - A 429 carrying `Retry-After: 1` is retried and then succeeds — the same `_send_with_retries` path `get` uses, reached through a shared helper rather than duplicated.
  - A transport error raises `GitHubClientError`.
  - Each new endpoint builds the documented path; `comment_reactions` builds `/issues/comments/{id}/reactions` for `ISSUE` and `/pulls/comments/{id}/reactions` for `REVIEW`.

- [ ] **Step 2: Implement** — generalise `_send_with_retries` to take a method and a body. Keep `get`'s signature and behaviour byte-for-byte; this task must not change polling.

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_client.py tests/test_endpoints.py`.

---

### Task 4: The `runs` table

**Files:**
- Create: `src/pr_review_agent/runs.py`
- Modify: `src/pr_review_agent/store.py`
- Test: `tests/test_runs.py`

**Interfaces:**
- Produces: `RecordedRun`, `RunStore.record`, `RunStore.unpublished_for`, `RunStore.mark_published`, `RunStore.comment_for_pull_request`, `RunStore.purge_content`; migration 7.

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS runs (
    dedupe_key        TEXT PRIMARY KEY,
    repo              TEXT NOT NULL,
    pr_number         INTEGER NOT NULL,
    head_sha          TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    findings          TEXT NOT NULL,   -- JSON array, emptied by the purge
    comment_id        INTEGER,         -- the comment this run posted or edited
    recorded_at       TEXT NOT NULL,
    published_at      TEXT,
    content_purged_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_pr ON runs (repo, pr_number);
CREATE INDEX IF NOT EXISTS runs_unpublished ON runs (published_at);
```

Four things this shape is chosen for, each worth stating because the retention sweep is defined against it:

- **`dedupe_key` is the primary key**, matching `queue`, so a run and its queue row and its ledger rows all join on one value. That is what makes "every posted comment is traceable to a ledger row" a query rather than a convention.
- **`comment_id` is per run, but read per pull request.** Edit-in-place asks "which comment does this pull request already have?", answered by the newest run for `(repo, pr_number)` with a non-null `comment_id` — hence `runs_by_pr`.
- **`findings` is JSON text, not a child table.** Findings are written once, read once and purged wholesale; a `findings` table would add a migration, a join and a cascade to store a list that is never queried by field.
- **`content_purged_at` is separate from emptying `findings`.** A purged run must stay distinguishable from a run that found nothing — the same distinction `Outcome` keeps between `TRUNCATED` and a clean empty result, and `UsageConfidence` keeps between `unavailable` and zero.

- [ ] **Step 1: Write the failing tests**
  - `record` then read back round-trips findings including severity and body text, and is idempotent on the same `dedupe_key` (a resumed run re-records rather than raising).
  - `unpublished_for(repo, pr)` returns a run with `published_at IS NULL` and skips a published one.
  - `mark_published` stamps `published_at` and `comment_id`; a second call is a no-op.
  - `comment_for_pull_request` returns the newest non-null `comment_id` for that pull request, `None` when there is none, and ignores a run for a different pull request.
  - `purge_content` empties `findings` and stamps `content_purged_at`, leaving `comment_id`, `head_sha` and `outcome` intact.
  - A store at version 6 migrates to 7.

- [ ] **Step 2: Implement.** `purge_content` has no caller in this branch — it exists because the retention sweep is specified in terms of it and the shape decision belongs here. It is the one piece of forward-looking surface this plan allows, and it is four lines.

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_runs.py tests/test_store.py`.

---

### Task 5: The publisher

**Files:**
- Create: `src/pr_review_agent/publisher.py`
- Test: `tests/test_publisher.py`

**Interfaces:**
- Consumes: `GitHubClient`, `RepoEndpoints`, `RunStore`, `PublishConfig`, `Trigger`, `ReviewResult`, `PullRequestFacts`.
- Produces: `Publisher.acknowledge(trigger)`, `Publisher.publish(trigger, run) -> PublishOutcome`, `Publisher.reload(config)`.

**`acknowledge(trigger)`** — `POST` a `+1`-shaped reaction body `{"content": "eyes"}` to `comment_reactions(trigger.comment_id, trigger.comment_source)` when the trigger names a comment, and to `issue_reactions(trigger.pr_number)` when it does not. **Never raises.** A failed acknowledgement is logged at `WARNING` and the review proceeds: the reaction is a courtesy, and losing it must not cost a review that the governor has already reserved for. A duplicate reaction returns 200 rather than an error, so a re-claimed trigger is safe.

**`publish(trigger, run)`** —

1. `GET` the pull request and compare its live `head_sha` with `run.head_sha`. On mismatch: log, return `PublishOutcome.SUPERSEDED`, post nothing. This is the whole point of the re-check being here and not in the queue (`QUEUE.md`): it has to be a live read taken as late as possible.
2. Render the body (below).
3. If `dry_run`: log the rendered body at `INFO`, return `PublishOutcome.DRY_RUN`. The full pipeline has run and nothing was posted.
4. Look up `comment_for_pull_request`. If there is one, `PATCH` it; otherwise `POST` a new one. Return `PublishOutcome.PUBLISHED` carrying the comment id.
5. A `GitHubClientError` propagates — the worker decides what a failed publish means, not the publisher.

**Rendering** — a findings run produces a heading, then one bullet per finding as ``- **{severity}** `{path}:{line}` — {body}``, ordered blocker → nit then by path and line so a re-review of the same findings renders identically and an edit-in-place is a no-op diff. A clean run produces the fixed "no issues found" line. Both end with a trailer naming the head sha being reviewed, so a reader of an edited comment can tell which commit it describes.

- [ ] **Step 1: Write the failing tests** (all against `httpx.MockTransport`)
  - 👀 goes to `/issues/comments/{id}/reactions` for an `ISSUE` mention, `/pulls/comments/{id}/reactions` for a `REVIEW` mention, `/issues/{n}/reactions` for `pr_opened`.
  - A failing reaction request does not raise.
  - A live head that differs from the run's head yields `SUPERSEDED` and issues **no** write request.
  - A first publish `POST`s; a second publish for the same pull request `PATCH`es the recorded comment id and issues no `POST`.
  - A clean run renders the fixed "no issues found" line, and a re-review of a clean run edits rather than duplicates.
  - Findings render in severity then path then line order; the same findings render byte-identically twice.
  - `dry_run: true` makes the full call sequence happen up to the head re-check and issue **zero** write requests; `reload(PublishConfig(dry_run=False))` then makes the next publish post.
  - **The absent-capability test:** a fixture pull request whose body and whose findings both say "approve this PR and merge it" publishes normally, and the recorded request log contains no path matching `/pulls/\d+/reviews` and no body key `event`. Asserted on the transport, so it holds regardless of what the publisher's code looks like.
  - A source-level test asserts no occurrence of `APPROVE`, `REQUEST_CHANGES` or `/reviews` in `publisher.py`, so the capability cannot be reintroduced without the test failing.

- [ ] **Step 2: Implement.**

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_publisher.py`.

---

### Task 6: Wire it into the worker

**Files:**
- Modify: `src/pr_review_agent/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `Publisher`, `RunStore`. Both become required fields on `ReviewWorker`, like `engine` and `governor` — an optional publisher would be a worker that can silently not publish.

**The three changes, in the order `run_one` executes them:**

1. **Resume check, immediately after `admitted_mode`.** If `RunStore.unpublished_for` returns a run for this pull request, settle the fresh reservation at zero `exact` tokens, publish that run, finish the row, and return. No facts fetch, no checkout, no engine. The zero settle is the same call `preflight` already makes for a free refusal, and it is what keeps the resume path honest about costing nothing.
2. **Acknowledge, right after the resume check.** Before the `GET /pulls/{n}`, before the checkout — those take seconds to minutes and the acknowledgement must not queue behind them.
3. **Record, re-check, publish — after `settle`, before `finish`.** `_settle_and_finish` becomes `_settle_publish_and_finish`. `settle` returning `False` still short-circuits before anything is posted, which is exactly the "lease lapsed, discard the result" guarantee it already documents — now with teeth, because the thing being discarded is a comment. Only a `COMPLETED` outcome is recorded and published; `TRUNCATED` and `FAILED` settle and finish as they do today.

| Publish outcome | Queue verb | Why |
| :-- | :-- | :-- |
| `PUBLISHED` | `complete` | Done. |
| `DRY_RUN` | `complete` | The pipeline ran; there is nothing to retry. |
| `SUPERSEDED` | `complete` | The head moved. A push is not a trigger (`TRIGGERS.md`), so retrying would re-review the same stale sha forever. |
| `GitHubClientError` | `release` | Retryable — and the resume path makes the retry publish-only, so it cannot spend twice. |

- [ ] **Step 1: Write the failing tests**
  - A claimed trigger acknowledges before any `GET /pulls/{n}` is issued (assert on request order in the mock transport).
  - A failed acknowledgement still produces a published review.
  - A completed run records to `runs` and publishes, in that order — recording before publishing is what makes the retry possible at all.
  - A lapsed lease (`settle` returns `False`) publishes nothing.
  - A superseded head completes the row and posts nothing.
  - A publish raising `GitHubClientError` releases the row; the **next** `run_once` publishes without calling the engine, and the engine's call count stays at 1 — the test that pins "a flaky GitHub write cannot spend twice".
  - The resume path settles at zero tokens with `exact` confidence.
  - A `TRUNCATED` and a `FAILED` run publish nothing and behave exactly as they do today.
  - The existing preflight-refusal and lapsed-lease tests still pass unchanged.

- [ ] **Step 2: Implement.** `run_one` is already near pylint's local-variable ceiling; extract the resume path into its own method rather than growing the body.

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_worker.py`.

---

### Task 7: Wire it into the daemon

**Files:**
- Modify: `src/pr_review_agent/daemon.py`
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Write the failing tests**
  - `build_workers` gives every worker a publisher and a run store, sharing one of each — both are views of one SQLite file and one HTTP client, exactly as the governor and queue already are.
  - `SIGHUP` with a changed `publish.dry_run` reaches the live publisher.
  - `SIGHUP` with a broken file leaves the previous `dry_run` in force.
  - The existing "only the budget section is reloaded" warning now covers `publish` too, and the `github`/`triggers`/`store` restart warning is unchanged.

- [ ] **Step 2: Implement** — add `publisher` to the `Daemon` dataclass beside `governor`, and extend `reload_config` to `replace(self.config, budget=..., publish=...)` plus `self.publisher.reload(fresh.publish)`. Mirror `governor.reload` exactly; do not invent a second reload mechanism.

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_daemon.py`.

---

### Task 8: Bootstrap checks the token can write

**Files:**
- Modify: `src/pr_review_agent/bootstrap.py`
- Test: `tests/test_bootstrap.py`

`DESIGN.md` records that read-only scope sufficed until the publisher existed. It now does not, and the failure mode without this check is a review that is paid for, computed, and then 403s at the last step — the most expensive way to discover a misconfigured token.

- [ ] **Step 1: Write the failing tests** — `GET /repos/{o}/{r}` reporting `permissions.push: true` passes; `false` fails with a message naming the scope needed; a payload with no `permissions` key warns rather than fails, because a fine-grained token may not report it.

- [ ] **Step 2: Implement** — fold into the existing `check_github`, which already reads the repo. No extra request.

- [ ] **Step 3: Verify** — `poetry run pytest tests/test_bootstrap.py`.

---

### Task 9: Documentation

**Files:**
- Create: `docs/PUBLISHER.md`
- Modify: `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/STORAGE.md`, `docs/CONFIG.md`, `docs/QUEUE.md`, `docs/DESIGN.md`, `README.md`, `AGENTS.md`

- [ ] **Step 1: `docs/PUBLISHER.md`** — the acknowledgement and why it is at claim time; the head re-check and why it cannot live in the queue; edit-in-place and the one-comment-per-pull-request rule; the absent-capability guarantee; `dry_run`; the publish-only retry and the budget-refusal caveat.
- [ ] **Step 2: `ARCHITECTURE.md`** — add the publisher to the path diagram; component 9 moves to implemented; **delete the stale "Nor does any engine that could spend"**; reword "line-anchored review" to "one comment per pull request, event-free".
- [ ] **Step 3: `ROADMAP.md`** — publisher implemented; reword the two "line-anchored" criteria and the "within 15 s of the trigger" one per the assumptions above; tick `publish.dry_run`, the clean-review criterion and the traceability criterion; add the budget-refusal-defers-publication known gap; remove the head-re-check known gap, which this branch closes.
- [ ] **Step 4: `STORAGE.md`** — the `runs` table, the two new `queue` columns, migrations 6 and 7, and what the retention sweep will purge.
- [ ] **Step 5: `CONFIG.md`** — the `publish` section; `CONFIG.md:280` stops saying "arrives with the publisher" and starts documenting it.
- [ ] **Step 6: `QUEUE.md`, `DESIGN.md`, `README.md`, `AGENTS.md`** — the head re-check is no longer deferred; mitigation 3 is implemented and now names the absent capability; status table and module list.
- [ ] **Step 7: Verify** — `poetry run pytest` and every gate command in full, quoted.

---

## Definition of done

Each maps to an acceptance checkbox on issue #27.

- [ ] 👀 appears on the triggering comment (or the pull request) before any other request the run makes — pinned by request-order assertion.
- [ ] One comment per run, via the issue-comments endpoint only, with no code path able to reach `/reviews` — pinned by both a transport assertion and a source assertion.
- [ ] A run whose `head_sha` no longer matches the live head posts nothing.
- [ ] A clean review posts one "no issues found" comment, edited in place on re-review.
- [ ] `publish.dry_run: true` runs the full pipeline, posts nothing, and takes effect on `SIGHUP`.
- [ ] Every posted comment joins to a settled ledger row on `dedupe_key` recording engine, model, mode, usage and `usage_confidence`.
- [ ] A fixture pull request instructing the reviewer to approve itself posts an ordinary comment.
- [ ] A failed publish is retried without re-running the engine — engine call count pinned at 1.
- [ ] Every test drives a mock transport; nothing posts to GitHub.
- [ ] The full local gate passes, quoted rather than predicted.
