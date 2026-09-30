---
name: pr-description
description: Write a pull-request description in the pr-review-agent house format — the change type, a one-paragraph summary, a table with one sentence per changed file, and how to test it — rendered by the same code `@claude describe` posts with. Use when asked what a PR, branch or diff does, to summarise a change for its description, or to draft a PR body. Not for reviewing: use review-report for findings.
---

# Writing a pull request description

This skill produces one artefact: a **pull request description** in the
format `pr_review_agent.description.render_description` emits, which is what
the agent posts when an allowlisted maintainer comments `@claude describe`.

Three properties matter:

- **It describes; it does not judge.** No findings, no verdict, no praise.
  If the request is "is this change right?", that is a review — use the
  `review-report` skill.
- **It says only what the code shows.** Read the files before describing
  them.
- **It is for a person to copy.** Nothing here edits a pull request or posts
  anything. The trailer says so and is not editable.

## The workflow

1. **Collect the facts you cannot guess.** Run
   `scripts/collect_context.py --pr <n>` (or `--base <ref>` outside a PR). It
   prints the head sha, merge base, commit count and changed paths. The
   header quotes the first and third; the paths are the rows the table needs.

2. **Read the diff from the merge base**, `git diff <merge_base>...HEAD`,
   and the files it touches. The merge base, not the base branch's tip: a
   base that moved on since the branch was cut would show its own commits
   as part of this change.

3. **Read `references/description-contract.md`.** It says what each field
   holds and what never goes in. The agent's own describe prompt is this
   same file.

4. **Write `description.json`** matching `assets/description.schema.json`.
   The prose goes in the fields. Do not write headings, the type label or
   the table by hand — those are the renderer's.

5. **Render.** `scripts/render_description.py description.json --context
   context.json` prints the description.

6. **Check.** `scripts/check_description.py <file>` fails on anything
   `references/layout-contract.md` forbids. Run it on a description edited
   by hand — that is the case it exists for.

## References

| File | Read it when |
|---|---|
| `references/description-contract.md` | Before writing. What each field holds, what never goes in. |
| `references/layout-contract.md` | When editing a rendered description by hand, or when a check fails. |

## Scripts

| Script | Does |
|---|---|
| `scripts/collect_context.py` | Header facts from git. The same script `review-report` ships, so each skill works installed alone. |
| `scripts/render_description.py` | `description.json` → the description, through the daemon's renderer. |
| `scripts/check_description.py` | Validates a rendered description. Exit 1 on violation. |

The last two import `pr_review_agent`. `skill install` copies the renderer
into `scripts/_vendor/`, so they work where the package is not installed; an
installed package wins over the copy. None of the scripts touch the network,
and none of them post anything.

## Worked example

`assets/description.example.json` rendered with `--pr 130 --head-sha
6a6d208… --commits 4` is `assets/description.example.md`, byte for byte.
