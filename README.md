# pr-review-agent

Locally-hosted LLM-based PR review agent with enforced usage budgets.

Outbound-only: the host receives no webhooks and opens no inbound port.
Reviews are triggered by polling the GitHub REST API.

## Status

Early. Only the trigger pipeline is implemented.

| Component | State |
| :--- | :--- |
| Classifier / allowlist / mention parsing | implemented, unit tested |
| Config loader (`config.yaml`) | implemented, unit tested |
| Poller (ETag conditional requests) | implemented, unit tested |
| Queue and per-PR lease | not started |
| Budget governor | not started |
| Engine adapter (`ReviewEngine`) | not started |
| Publisher | not started |
| Retention sweep | not started |

Phase ordering is deliberate: the **budget governor lands before the review
worker**, so the spending rails exist before anything can spend.

## Trigger rules

Exactly two events start a review:

1. a freshly opened PR whose **author** is allowlisted;
2. a comment containing `@claude` whose **commenter** is allowlisted.

Gating the mention on the commenter is what lets a maintainer summon a
review of an outside contribution that would not be auto-reviewed.

Deliberately rejected:

| Event | Reason code |
| :--- | :--- |
| Push to an existing PR | never classified — the poller emits no push event |
| Draft PR | `draft` |
| PR from an unlisted author | `author_not_allowlisted` |
| Comment from an unlisted account | `commenter_not_allowlisted` |
| Any bot, including the agent itself | `bot_author` / `bot_commenter` |
| `@claude` in a fence, code span or blockquote | `no_mention` |
| Already-open PR seen on first poll | `not_fresh` |

### Two decisions worth knowing

**Allowlisting is on the numeric user id, never the login.** A login can be
renamed and the freed name registered by somebody else, which would silently
transfer eligibility to a stranger. A login in the config raises
`AllowlistConfigError` at startup rather than never matching.

**The cold-start watermark (`Classifier.since`) is load-bearing.** The poller
sees *open* PRs, not `opened` webhook events, so without a watermark the
first poll would treat the entire open backlog as fresh and review all of it
at once — burning the weekly allowance in a single pass.

## Poller

Polls three **repo-wide** endpoints, not per-PR — one request per endpoint
per cycle regardless of how many PRs are open, which is what keeps the
rate-limit math in the issue (1,080 requests/hour at a 10 s interval) true:

    GET /repos/{owner}/{repo}/pulls?state=open              (open PRs)
    GET /repos/{owner}/{repo}/issues/comments                (PR conversation comments)
    GET /repos/{owner}/{repo}/pulls/comments                  (inline diff comments)

Each is polled with an `If-None-Match` conditional GET. A `304` costs
nothing against GitHub's rate limit and does not reset the adaptive
interval; any `200` (something changed) snaps the interval straight back to
the 10 s floor, decaying by 2x per quiet cycle up to a 600 s ceiling.

The ETag cache is in-memory only (`ETagStore`) — a restart costs one extra
full poll per endpoint, not correctness. Persistence lands with the SQLite
store in the queue/budget-governor phase.

**Not yet built:** turning a raw poll payload into `PullRequest` / `Comment`
objects and feeding them to the `Classifier`, and the loop that actually
calls `poll_once()` on a schedule. Both are pure wiring around what exists.

### Dedupe keys

    pr_opened:{repo}:{pr}:{head_sha}
    mention:{repo}:{pr}:{comment_id}

The mention key excludes `head_sha` on purpose: the same comment must never
trigger twice, and a subsequent push must not revive it.

## Development

```bash
python -m venv venv
./venv/Scripts/python.exe -m pip install -e ".[dev]"   # POSIX: venv/bin/python
./venv/Scripts/python.exe -m pytest -q
./venv/Scripts/python.exe -m ruff check . && ./venv/Scripts/python.exe -m ruff format --check .
```

Copy `config.example.yaml` to `config.yaml` before running the daemon.
`config.yaml` is gitignored: it names real accounts and will later sit
beside the agent's credentials.

The trigger pipeline is pure functions over fixtures — the suite needs no
network and spends no tokens.

## Budget governor

Because billing is **subscription** mode, the budget governor is not
optional. All Claude surfaces share one usage pool, so an unbounded reviewer
does not merely overspend, it locks the host operator out of their own
interactive Claude Code sessions until the window resets. `reviewer_share_pct`
caps the agent below the plan ceiling for exactly this reason.

## Still to confirm

1. **Outbound HTTPS from the deployment host** to `api.github.com`,
   `github.com`, `codeload.github.com` and `api.anthropic.com`. The poller
   only needs `api.github.com`; run it against the real DTaaS repo once a
   token exists to confirm the host can reach it at all.
2. **The reviewer account** the agent posts as. Its numeric id goes in
   `github.agent_user_id`; until it is set, the agent cannot recognise and
   skip its own comments.
3. **Remaining allowlist members** — currently only `114395272` (8ohamed).
4. **A GitHub token for the poller.** Read-only access to the three
   endpoints is enough for polling; write scope is only needed once the
   publisher exists.

