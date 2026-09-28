# The review skill

The report format is not only for the daemon. `pr-review-agent skill install`
copies a Claude Code **skill** into `~/.claude/skills`, so that a review you
write by hand — on this repository or any other — comes out in the same shape
as one the agent posts.

```bash
pr-review-agent skill install          # or --dir <repo>/.claude/skills
```

Then start a Claude Code session and ask for a review. The skill supplies:

| Part | What it is |
| --- | --- |
| `SKILL.md` | The workflow: collect the header facts, read the contract, sweep, discard, write findings as JSON, render, check. |
| `references/finding-contract.md` | What is in scope, what to sweep, how a title and a body are written, what each severity means. The same file `engine/prompt.py` reads. |
| `references/report-contract.md` | The rendering rules — sections, ordering, numbering, the empty report, the trailer. See [Report template](review-report.md). |
| `references/false-positives.md` | What not to report. The other file `engine/prompt.py` reads. |
| `scripts/collect_context.py` | Head sha, merge base, commit count and changed paths, out of git. |
| `scripts/render_report.py` | `findings.json` → a report, through `publisher.render` itself. |
| `scripts/check_report.py` | A hand-written or hand-edited report, checked against the contract. |
| `assets/` | The findings schema, an example findings file, and the report it renders to. |

Nothing in the skill posts anything. It writes files and prints them;
publishing stays a separate, explicit act. `render_report.py` writes to
`--out` or to stdout, `check_report.py` prints violations and exits 1, and
`collect_context.py` prints JSON. None of them opens a socket, touches the
daemon's database or knows a GitHub token exists — getting a report onto a
pull request means pasting it, or `gh pr comment --body-file`.

## What the scripts need

`collect_context.py` is stdlib-only. `render_report.py` and
`check_report.py` import `pr_review_agent`, because they call the renderer
and the numbering the daemon calls rather than a copy of them.

`skill install` copies files; it does not install the package into the
interpreter that will run them, and in a pipx or poetry layout those are
different interpreters. Both scripts detect it and print what to install.

## Why the daemon does not load it

The daemon's own reviewer runs `claude` with `--setting-sources ""`,
`--restricted` and `--disable-slash-commands`. Between them, no settings
file, plugin or skill is discovered — which is the point: a reviewer that
loads configuration out of the tree it is reviewing takes its instructions
from whoever opened the pull request. Three of the four injection defences in
[DESIGN.md](../DESIGN.md) are those flags, and none of them will be relaxed to
make a directory loadable.

The engine is given the same text by a route that widens nothing:
`engine/prompt.py` reads `finding-contract.md` and `false-positives.md` out
of the package and splices them into the prompt. `report-contract.md` is not
sent and does not need to be — the sections, the ordering, the numbering and
the trailer are `publisher.render`'s, decided in code, so the daemon's report
format does not depend on a model complying with a description of it. If a future release runs the engine inside bubblewrap
([roadmap A2](../FEATURE-ROADMAP.md)), that route still works unchanged — the
text is already in the process, so there is no directory for the sandbox
profile to bind and nothing new for it to allow.
