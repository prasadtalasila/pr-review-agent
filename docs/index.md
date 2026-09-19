# pr-review-agent

A locally-hosted LLM pull request reviewer that cannot overspend and cannot
be summoned by a stranger.

## What it is

A single Python asyncio daemon with a SQLite store, running on a private host.
It polls on pull requests of a GitHub repository for exactly two events:

1. a freshly opened pull request whose author is pre-approved;
2. a comment containing `@claude` whose commenter is pre-approved.

No pull request leaves our infrastructure, and the publisher takes no approval
or merge action regardless of what a review concludes — a machine's judgement
can never block a merge. [DESIGN.md](DESIGN.md) has the constraints that
shaped this design and every alternative that was turned down;
[ARCHITECTURE.md](ARCHITECTURE.md) has the components that implement it.

Source, the quickstart and the licence are on
[GitHub](https://github.com/prasadtalasila/pr-review-agent).
