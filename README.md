# pr-review-agent

<p align="center">
<b>A locally-hosted LLM pull request reviewer that cannot overspend and cannot
be summoned by a stranger.</b>
</p>

<p align="center">
Outbound-only: the host receives no webhooks and opens no inbound port.
Reviews are triggered by polling the GitHub REST API.
</p>

## 🔑 The one rule

> **Nothing may call a review engine outside the budget governor.**

The backend is a subscription plan, and **all Claude surfaces share one usage
pool**. An unbounded reviewer does not merely overspend — it locks maintainers
out of their own interactive Claude Code sessions until the window resets.

So the agent is capped at a *share* of each plan window, never the whole
allowance, and the phase order puts the **budget governor before the review
worker**: the spending rails exist before anything can spend. Everything in
[docs/BUDGET.md](docs/BUDGET.md) follows from that one sentence.

- [What it is](#-what-it-is)
- [What starts a review](#-what-starts-a-review)
- [How it finds out](#-how-it-finds-out)
- [Status](#-status)
- [Quickstart](#-quickstart)
- [Documentation](#-documentation)

## ✍ What it is

A single Python asyncio daemon with a SQLite store, running on a private host.
It polls GitHub for two events, decides whether either may start a review, and
— once the remaining components land — runs the review locally and posts one
line-anchored comment.

Four constraints shaped it, and each one removes a category of off-the-shelf
solution:

1. **No inbound network access.** The host cannot receive webhooks, so every
   webhook-driven integration is out.
2. **Restricted eligibility.** Only configured contributors are auto-reviewed;
   others are reviewed when an allowlisted maintainer asks.
3. **A hard usage ceiling with no overspend**, on a shared pool.
4. **Opaque limits.** Subscription plans expose no quota API, so budget
   enforcement is self-maintained rather than queried.

No pull request leaves our infrastructure, and the publisher takes no approval
or merge action regardless of what a review concludes — a machine's judgement
can never block a merge. [docs/DESIGN.md](docs/DESIGN.md) has the full
reasoning and every alternative that was turned down.

## ✅ What starts a review

Exactly two events:

1. a freshly opened pull request whose **author** is allowlisted;
2. a comment containing `@claude` whose **commenter** is allowlisted.

Gating the mention on the commenter is what lets a maintainer summon a review
of an outside contribution that would not be auto-reviewed.

Everything else is rejected with a reason code that says which rule fired:

| Event | Reason code |
| :-- | :-- |
| Push to an existing pull request | *never classified* — the poller emits no push event |
| Draft pull request | `draft` |
| Pull request from an unlisted author | `author_not_allowlisted` |
| Comment from an unlisted account | `commenter_not_allowlisted` |
| Any bot | `bot_author` / `bot_commenter` |
| The agent's own account | `self_author` / `self_commenter` |
| `@claude` in a fence, code span or blockquote | `no_mention` |
| Already-open pull request below the watermark | `not_fresh` |

Two decisions carry most of the weight:

**Allowlisting is on the numeric user id, never the login.** A login can be
renamed and the freed name registered by somebody else, which would silently
transfer eligibility to a stranger. A login in the config fails at startup
rather than never matching.

**The cold-start watermark is load-bearing.** The poller sees *open* pull
requests, not `opened` events, so without a watermark the first poll would
treat the entire open backlog as fresh and review all of it at once — burning
the weekly allowance in a single pass. It is persisted, and only ever moves
forward.

[docs/TRIGGERS.md](docs/TRIGGERS.md) has the rest, including what counts as a
mention and why the dedupe keys are shaped the way they are.

## 📡 How it finds out

Three **repo-wide** endpoints, polled with `If-None-Match`:

```text
GET /repos/{owner}/{repo}/pulls?state=open&…&per_page=100
GET /repos/{owner}/{repo}/issues/comments?…&per_page=100
GET /repos/{owner}/{repo}/pulls/comments?…&per_page=100
```

One request per endpoint per cycle regardless of how many pull requests are
open — 1,080 requests/hour at a 10 s interval, against a budget of at least
5,000. A `304` costs nothing against the rate limit and decays the interval
towards a 600 s ceiling; any `200` snaps it straight back to the 10 s floor.
If the remaining budget nears exhaustion, the interval is held at the ceiling
whatever else happened.

[docs/POLLER.md](docs/POLLER.md) covers the sort-order invariant the design
rests on, the retry rules, and why the notifications API was not used.

## 📊 Status

Early. The trigger pipeline, the poller and the persistence layer are
implemented and unit tested; nothing posts to GitHub yet.

| Component | State |
| :-- | :-- |
| Classifier / allowlist / mention parsing | implemented |
| Config loader (`config.yaml`) | implemented |
| Poller (async, ETag conditional requests) | implemented |
| Payload mapping (REST dicts → trigger models) | implemented |
| SQLite watermark and ETag store | implemented |
| Daemon loop, queue and per-PR lease | not started |
| Budget governor | not started |
| Engine adapter (`ReviewEngine`) | not started |
| Publisher | not started |
| Retention sweep | not started |

[docs/ROADMAP.md](docs/ROADMAP.md) has the build order and the acceptance
checklist.

## 🚀 Quickstart

Python **3.10 – 3.14**. Dependencies are managed with
[Poetry](https://python-poetry.org/docs/), installed **inside the project
venv** — a system-wide Poetry is not supported
([why](DEVELOPER.md#why-not-a-system-wide-poetry)).

```bash
git clone https://github.com/prasadtalasila/pr-review-agent
cd pr-review-agent

python -m venv .venv
.venv/bin/python -m pip install --upgrade pip poetry
export PATH="$PWD/.venv/bin:$PATH"      # so `poetry` is the project's copy
command -v poetry                       # must print <repo>/.venv/bin/poetry

poetry install
poetry run pytest

cp config.example.yaml config.yaml      # then edit: repo, allowlist, agent id
```

The suite needs no network and spends no tokens: the trigger pipeline is pure
functions over fixtures, and the poller tests drive `httpx.MockTransport`.

`config.yaml` is gitignored — it names real accounts and will later sit beside
the agent's credentials. [docs/CONFIG.md](docs/CONFIG.md) documents every key.

## 🗂 Documentation

| Document | Answers |
| :-- | :-- |
| [docs/DESIGN.md](docs/DESIGN.md) | Why does this exist and why is it shaped like this? The four constraints, every alternative considered and rejected, the billing-mode question that is still open, and how prompt injection is handled |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | What actually runs? The seven components, the path an event takes, the package layout, and which layer may import which |
| [docs/TRIGGERS.md](docs/TRIGGERS.md) | What starts a review and what does not? Every reason code and its log level, what counts as a mention, the dedupe keys, and why identity is a number |
| [docs/POLLER.md](docs/POLLER.md) | How does it learn something happened without an inbound port? The three endpoints, the rate-limit arithmetic, the adaptive interval, the retry rules, and how a comment payload is mapped to a pull request |
| [docs/STORAGE.md](docs/STORAGE.md) | What has to survive a restart, and what does a lost watermark actually cost? Why SQLite, and why a watermark only moves forward |
| [docs/BUDGET.md](docs/BUDGET.md) | **Specification, not yet built.** The five enforcement layers, reserve-then-settle under concurrency, the degradation ladder, and the self-calibrating breaker |
| [docs/CONFIG.md](docs/CONFIG.md) | What settings exist, what does each accept, and why are unknown keys an error? |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is built, what is next, the acceptance checklist, and the known gaps |
| [DEVELOPER.md](DEVELOPER.md) | How do I set up, test, lint and build this? |
| [CLAUDE.md](CLAUDE.md) | The behavioural guidelines applied to every change |
| [AGENTS.md](AGENTS.md) | The coding-assistant conventions |

## 📄 Licence

MIT. See [LICENSE](LICENSE).
