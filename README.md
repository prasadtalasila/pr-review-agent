# pr-review-agent

<p align="center">
<b>A locally-hosted LLM pull request reviewer that cannot overspend and cannot
be summoned by a stranger.</b>
</p>

- [What it is](#-what-it-is)
- [Quickstart](#-quickstart)
- [Documentation](#-documentation)

## ✍ What it is

A single Python asyncio daemon with a SQLite store, running on a private host.
It polls on pull requests of a GitHub repository for exactly two events:

1. a freshly opened pull request whose author is pre-approved;
2. a comment containing `@claude` whose commenter is pre-approved.

No pull request leaves our infrastructure, and the publisher takes no approval
or merge action regardless of what a review concludes — a machine's judgement
can never block a merge. [docs/DESIGN.md](docs/DESIGN.md) has the constraints
that shaped this design and every alternative that was turned down;
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the components that
implement it.

## 🚀 Quickstart

Python **3.10 – 3.14**, via [Poetry](https://python-poetry.org/docs/) —
see [DEVELOPER.md](DEVELOPER.md) for the full setup, including why Poetry
must live inside the project venv.

```bash
git clone https://github.com/prasadtalasila/pr-review-agent
cd pr-review-agent

python -m venv .venv
.venv/bin/python -m pip install --upgrade pip poetry
export PATH="$PWD/.venv/bin:$PATH"
poetry install

cp config.minimal.example.yaml config.yaml   # then edit: repo, agent_user_id,
                                             # allowlist, budget

GITHUB_TOKEN=... poetry run python -m pr_review_agent.daemon
```

`config.yaml` is gitignored — it names real accounts and will later sit beside
the agent's credentials. [docs/CONFIG.md](docs/CONFIG.md) documents every key,
and [DEVELOPER.md](DEVELOPER.md#-bootstrap-checks) covers the bootstrap check
worth running before a first deploy on a new host.

## 🗂 Documentation

| Document | Answers |
| :-- | :-- |
| [docs/DESIGN.md](docs/DESIGN.md) | Why does this exist and why is it shaped like this? The four constraints, every alternative considered and rejected, the billing-mode question that is still open, and how prompt injection is handled |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | What actually runs? The components, the path an event takes, the package layout, and which layer may import which |
| [docs/TRIGGERS.md](docs/TRIGGERS.md) | What starts a review and what does not? Every reason code and its log level, what counts as a mention, the dedupe keys, and why identity is a number |
| [docs/POLLER.md](docs/POLLER.md) | How does it learn something happened without an inbound port? The three endpoints, the rate-limit arithmetic, the adaptive interval, the retry rules, and how a comment payload is mapped to a pull request |
| [docs/DAEMON.md](docs/DAEMON.md) | What runs continuously, and what is it careful not to do? The cycle, the cold-start spend bound, the two watermark ordering rules, and how it shuts down |
| [docs/QUEUE.md](docs/QUEUE.md) | Where does an accepted trigger wait, and what stops one review being paid for twice? Dedupe, the per-pull-request lease, why leases expire instead of renewing, and the retry bound |
| [docs/STORAGE.md](docs/STORAGE.md) | What has to survive a restart, and what does a lost watermark actually cost? Why SQLite, and why a watermark only moves forward |
| [docs/WORKSPACE.md](docs/WORKSPACE.md) | How does a pull request's code get onto disk, and why is none of it ever run? The bare mirror, the per-run worktree, the untrusted-tree hardening, and the diff-size caps |
| [docs/BUDGET.md](docs/BUDGET.md) | The rolling windows and the share that guarantees human headroom, reserve-then-settle under concurrency, the degradation ladder, the circuit breaker, and what is still not built |
| [docs/WORKER.md](docs/WORKER.md) | What drains the queue? The claim-run-settle loop, what a failed run settles at and why, which failures retry and which are permanent, what a run leaves behind, the supervisor, and why not a process per review |
| [docs/PUBLISHER.md](docs/PUBLISHER.md) | How does a review become visible, and what stops the agent approving anything? The 👀 at claim time, the live `head_sha` re-check, one comment per pull request, `publish.dry_run`, and why a failed publish never costs a second review |
| [docs/ENGINE.md](docs/ENGINE.md) | How does a different coding agent plug in? The one swappable step, what an engine is given and must return, the capability record, and why every adapter is a CLI subprocess rather than an SDK |
| [docs/CONFIG.md](docs/CONFIG.md) | What settings exist, what does each accept, and why are unknown keys an error? |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is built, what is next, the acceptance checklist, and the known gaps |
| [DEVELOPER.md](DEVELOPER.md) | How do I set up, test, lint and build this? |
| [CLAUDE.md](CLAUDE.md) | The behavioural guidelines applied to every change |
| [AGENTS.md](AGENTS.md) | The coding-assistant conventions |

## 📄 Licence

MIT. See [LICENSE](LICENSE).
