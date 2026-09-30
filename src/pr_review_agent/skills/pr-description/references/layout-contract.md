# The layout contract

What `description.render_description` produces, stated as rules so that a
description written by hand can be checked against the same ones. A rule
named in **bold** is the identifier `scripts/check_description.py` prints
when it is broken. One rule is not checked: **describe-not-review** is a
sentence rather than a shape.

## Skeleton

```markdown
## Description: PR #<number> (`<sha7>`, <c> commits)

**Type:** <Bug fix | Enhancement | Refactor | Documentation | Tests | Other>

<summary paragraph>

## Changes

| File | Change |
| :-- | :-- |
| `<path>` | <one sentence> |

## How to test

<how to check the change works>

<sub>Automated description. It takes no action on this pull request beyond this comment; copy what is useful into the description.</sub>
```

## Rules

**header** — always first, and names the head sha, because a pull request
has several revisions and a reader has to know which one this describes.
`collect_context.py` prints the three values.

**type** — exactly one `**Type:**` line, with one of the six labels.

**headings** — `## Changes` then `## How to test`, each once, and no others.
The summary has no heading of its own: it is the paragraph a reader copies.

**table** — under Changes, a two-column table with one row per file entry,
paths as code spans. A pipe or a newline in a cell is escaped or folded by
the renderer; a hand-written table has to do the same. A description with no
file entries says `_No changed files were described._` instead. When the
table does not fit a GitHub comment, rows are dropped from the end and
counted in one line below it.

**trailer** — verbatim, last, with nothing after it. It says the agent did
nothing but post the comment, and that the pull request body is still the
contributor's.

**length** — within `MAX_BODY_CHARS`, the size the publisher renders to.

**describe-not-review** — no findings, severities, verdicts or praise. See
`description-contract.md`.
