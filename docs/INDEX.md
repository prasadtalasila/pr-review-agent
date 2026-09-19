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

pip install --upgrade pip poetry
poetry install

cp config.minimal.example.yaml config.yaml
# update config
# see full config in config.example.yaml

# get GitHub PAT with read and write permissions on pull requests
GITHUB_TOKEN=xxxx poetry run python -m pr_review_agent.daemon
```
