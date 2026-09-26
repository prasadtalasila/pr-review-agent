# Recorded GitHub API payloads

Responses from the real GitHub REST API, kept so that
[`tests/test_integration_recorded.py`](../../test_integration_recorded.py) can
drive a whole `Daemon.run_once` against the shapes GitHub actually sends rather
than against dictionaries a test author wrote from memory. Nothing here needs a
network or a token: `httpx.MockTransport` serves them.

Recorded from `prasadtalasila/pr-review-agent` — a public repository — on
**2026-09-26**.

| File | Endpoint |
| --- | --- |
| `pulls_open.json` | `GET /repos/{o}/{r}/pulls?state=open&sort=created&direction=desc&per_page=100` |
| `issue_comments.json` | `GET /repos/{o}/{r}/issues/comments?sort=updated&direction=desc&per_page=100` |
| `review_comments.json` | `GET /repos/{o}/{r}/pulls/comments?sort=updated&direction=desc&per_page=100` |
| `pull.json` | `GET /repos/{o}/{r}/pulls/93` |

## What was edited, and what was not

Every object keeps all of its recorded fields. The edits are these, and there
are no others:

- **Truncation.** The pulls page is the two newest of three; the comments page
  is five of twenty-four, chosen to keep the mix the filter has to tell apart
  (three comments on plain issues, one on a closed pull request, one on an open
  one). Truncating a page is what a smaller repository would have returned.
- **One body, and one comment's target.** Nothing in this repository's real
  history mentions the agent's handle, and the accept path is the one worth
  driving end to end. So the oldest of the two pull-request comments has its
  `body` replaced with `@claude please take a look at this one.` and its
  `issue_url` and `html_url` retargeted from pull request 60 to pull request
  95, which the recorded pulls page reports as open. Its other twenty fields —
  ids, timestamps, the author object, the reaction rollup — are as recorded.

`review_comments.json` is an empty array because the repository genuinely had
no inline diff comments. That is a recording, not a placeholder: an empty page
is a case the poller has to handle.

No credential was recorded. These endpoints return public data and the request
headers, which carry the token, are not part of a response.

## Re-recording

`test_the_recorded_fixtures_are_what_this_module_assumes` pins the mix the rest
of the module reads — pull request numbers, draft flags, how many comments sit
on pull requests. A re-recording that changes any of those fails there first,
which is the intended place to find out.
