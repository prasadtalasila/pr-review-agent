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
