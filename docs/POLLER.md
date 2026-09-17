# Poller

How the agent learns that something happened, without GitHub being able to
reach it. Implemented in `src/pr_review_agent/poller/`.

## 🌐 Three repo-wide endpoints

```text
GET /repos/{owner}/{repo}/pulls?state=open&sort=created&direction=desc&per_page=100
GET /repos/{owner}/{repo}/issues/comments?sort=updated&direction=desc&per_page=100
GET /repos/{owner}/{repo}/pulls/comments?sort=updated&direction=desc&per_page=100
```

Repo-wide, not per-PR. A per-PR poll would mean one request per open pull
request per cycle, which does not scale and defeats the point of conditional
requests. Three repo-wide endpoints is **one request per endpoint per cycle
regardless of how many pull requests are open**, which is what keeps the
rate-limit arithmetic below true.

**The sort order is an invariant, not a preference.** Every path is
newest-first and capped at `per_page=100`, so anything the poller has not seen
yet is on page 1 — which is why the poller never paginates. Changing the sort
would silently hide new events behind a page boundary, and nothing would look
broken.

## 📉 Rate-limit arithmetic

Three endpoints at a 10 s interval is **1,080 requests/hour per repository**
against a GitHub App installation budget of at least 5,000/hour — and that is
the worst case in which every response is a `200`. With ETags, idle polling
consumes essentially none of the budget. Secondary limits (900 points/min per
endpoint, 100 concurrent requests) are three orders of magnitude away.

## 🪶 Conditional GETs

Each endpoint is polled with an `If-None-Match` header carrying the last ETag
seen for that path. A `304` returns an empty body, **costs nothing against the
rate limit**, and does not reset the adaptive interval. Only a `200` —
something changed — or an error costs budget.

The ETag cache is keyed by request path. Losing it costs one extra full GET
per endpoint, not correctness; see [STORAGE.md](STORAGE.md) for where it is
kept.

## ⏱ The adaptive interval

| Event | Effect |
| :-- | :-- |
| Any endpoint returns `200` | snap straight to the 10 s floor |
| All three return `304` | multiply by 2, up to the 600 s ceiling |
| `x-ratelimit-remaining` at or below 50 | force the ceiling, whatever else happened |

Snapping to the floor rather than easing down keeps detection latency low right
when a repository just became active, which is when it matters. The interval is
adjusted **once at the end of a full sweep**, from whether *any* endpoint
changed: an active comments endpoint should keep the whole repository polling
fast even if open-PRs itself is quiet.

The budget floor takes priority over everything, including a `200`. An active
repository is not worth chasing at the cost of running out of requests before
the window resets. `lowest_remaining` is read from `304` responses too, which is
correct — GitHub sends the rate-limit headers on a `304`.

**Release is implicit and deliberate:** nothing un-forces the ceiling. The next
cycle whose `remaining` is above the floor goes back through the normal
`record()` path and may snap straight to the floor again. The force is a
per-cycle decision, not a latched state.

## 🔁 Rate limits and retries

`Retry-After` is GitHub's own signal for a transient (usually
secondary/abuse) rate limit. `GitHubClient.get` retries such a response a
bounded number of times and raises anything else immediately.

Three responses deliberately fall through to the error path:

- **A permanent 403** — bad token, no access. It never carries `Retry-After`,
  which is exactly what distinguishes it. Retrying a bad token forever is the
  failure mode `test_plain_permission_403_is_not_retried` exists to prevent.
- **A cooldown longer than 60 s.** GitHub may ask for an hour. Holding the poll
  loop that long is the poller's decision to make, not the client's, so a
  longer cooldown is returned un-retried.
- **The *primary* rate limit.** It returns 403/429 with
  `x-ratelimit-remaining: 0` and `x-ratelimit-reset` but **no** `Retry-After`,
  so it falls through to the error. That is defensible because the budget floor
  above should prevent ever reaching it — reaching the primary limit at all
  means the floor was set too low. Reading `x-ratelimit-reset` so the poller can
  sleep to the reset is a possible later refinement.

RFC 9110 permits `Retry-After` to be either a delay in seconds or an HTTP date.
GitHub sends seconds today, but reading "come back at 07:28" as "retry in 1 s"
would hammer the endpoint that just asked for room, so the date form is parsed
as a deadline. A header that is unparseable, in the past, or over the cap
yields no retry at all.

## 🗺 Mapping a payload to a pull request

`payloads.py` is the seam between raw GitHub dicts and the dataclasses the
classifier consumes. Three quirks of the REST shape decide its design.

**A comment payload does not name its pull request.** `/issues/comments`
carries an `issue_url`; `/pulls/comments` carries a `pull_request_url`. The
number is the last path segment of whichever is present, so it costs no extra
request.

**A comment payload does not distinguish a pull request from an issue.** The
issues endpoint returns both, and reviewing issue #7 because someone wrote
`@claude` in it would be wrong. A comment on a pull request has an `html_url`
under `/pull/`; a plain issue comment does not. That field is already in the
payload, so the filter is free.

**A comment payload carries no head SHA.** `head_sha` is therefore left
unresolved and read when the trigger is claimed. Resolving it at poll time
would cost one request per comment on every cycle to learn a value that can go
stale before the worker starts — and the worker already re-reads it immediately
before posting, which is the only read that counts. Review comments do carry a
`commit_id`, but it names the commit the comment was *written against* rather
than the pull request's head, so it is not used either.

Consequently `Comment.head_sha` and `Trigger.head_sha` are `str | None`. The
mention dedupe key never included `head_sha` anyway, so nothing downstream
regresses.

## 📬 Why not the notifications API

A reasonable alternative is `GET /notifications`: one endpoint instead of
three, with GitHub's own `X-Poll-Interval` telling the client how often to
come back. It was not used for two reasons.

It needs a **user** token rather than an App installation token, which puts a
person's credential on the host and ties the agent's reach to that person's
account rather than to an installation. And it only reports activity on
**subscribed** threads, so a freshly opened pull request from an allowlisted
author — the primary trigger — does not appear until something subscribes the
account to it. Working around that means polling pull requests anyway, at which
point the notifications endpoint is a fourth request rather than a replacement
for three.

Worth revisiting only if the repository count grows enough that three requests
per repository per cycle starts to matter.
