# pr-review-agent

<p align="center">
<b>A locally-hosted LLM pull request reviewer that cannot overspend and cannot
be summoned by a stranger.</b>
</p>

A single Python asyncio daemon with a SQLite store, running on a private host.
It polls on pull requests of a GitHub repository for exactly two events:

1. a freshly opened pull request whose author is pre-approved;
2. a comment containing `@claude` whose commenter is pre-approved.

and performs code review using Claude CLI and posts review comments on the pull request.

## 🚀 Quickstart

_Requires_: Python **3.10 – 3.14** and python virtualenv.
Download [latest release](https://github.com/prasadtalasila/pr-review-agent/releases). 

```bash
python -m venv .venv
source .venv/bin/activate

# download the latest release
pip install pr_review_agent-<version>-py3-none-any.whl

pr-review-agent config generate      # writes ./config.yaml
# update config; `config generate --full` writes the commented template
pr-review-agent config validate

# get GitHub PAT with read and write permissions on pull requests
GITHUB_TOKEN=xxxx pr-review-agent host check            # can this host reach it all?
GITHUB_TOKEN=xxxx pr-review-agent daemon start          # reads ./config.yaml
GITHUB_TOKEN=xxxx pr-review-agent daemon start --config /etc/pr-review-agent/config.yaml
```

Installing the package puts `pr-review-agent` on the path. Commands follow a
`pr-review-agent <noun> <verb>` grammar, grouped by the setup workflow:
`config` → `host` → `daemon`. `--config` is optional: without it a command
reads `config.yaml` from the directory it is started in, which is also where
`state.db` is written.

## 🗂 Documentation

The links below are relative, which is what works on GitHub and in the
[documentation site](https://prasad.talasila.in/pr-review-agent/). They are
not what PyPI gets: a relative target there resolves against `pypi.org` and
404s, so the release workflow builds from
[`scripts/pypi_readme.py`](scripts/pypi_readme.py)'s rewrite of this file,
with every target made absolute and pinned to the tag being released.

| Document | Answers |
| :-- | :-- |
| [docs/CONFIG.md](docs/CONFIG.md) | What settings exist, what does each accept, and why are unknown keys an error? |
| [docs/SERVICE.md](docs/SERVICE.md) | How is it deployed? The systemd user unit, why a user unit and not a system one, the three paths that must be absolute, lingering, and what `systemctl reload` reloads |
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
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is built, what is next, the acceptance checklist, and the known gaps |
| [docs/FEATURE-ROADMAP.md](docs/FEATURE-ROADMAP.md) | What could be built next, drawn from five neighbouring projects and a hardening review, each candidate with its cost |
| [DEVELOPER.md](DEVELOPER.md) | How do I set up, test, lint and build this? |
| [DOCKER.md](DOCKER.md) | How do I get all of that without installing any of it? The development container, what it carries, and the two things about it that are not obvious |
| [CLAUDE.md](CLAUDE.md) | The behavioural guidelines applied to every change |
| [AGENTS.md](AGENTS.md) | The coding-assistant conventions |
