---
name: review-report
description: Write a pull-request review in the pr-review-agent house format — findings that state a consequence, evidence quoted from files you have read, a remedy, and a report rendered into Blocking / Should fix / Nits with stable cross-round numbering. Use when reviewing a PR, a diff, a branch or a working tree and the output is a review comment, a review document, or a set of review issues. Also use when asked to check, fix or re-render an existing review so it matches the contract.
---

# Writing a review

This skill produces one artefact: a **review report** in the format
`pr_review_agent.report.render` emits. The format is not decoration. Three
of its properties are load-bearing, and a review that drops them is worse than
no review:

- **A title states a consequence.** It is read on its own, first, and often
  instead of the body.
- **Numbers are stable across rounds, and gaps are information.** A vacant
  number says an earlier finding was fixed. Renumbering destroys that.
- **The report takes no action.** No verdict, no approval, no merge
  recommendation. The trailer says so and is not editable.

## When not to use this

Do not use it to summarise a diff, to praise a change, or to answer "should
this merge?". Those are not reviews. If the request is "what does this PR do",
answer in prose and stop.

## The workflow

1. **Collect the facts you cannot guess.** Run
   `scripts/collect_context.py --pr <n>` (or `--base <ref>` outside a PR). It
   prints the head sha, merge base, commit count, round number and the list of
   changed paths. The header of every report quotes these; getting the sha
   wrong makes an edited-in-place comment describe the wrong revision, which
   is the one way this format can mislead.

2. **Read `references/finding-contract.md` before you look at the diff.** It
   is what separates a finding from an observation: the scope rule (causation,
   not curiosity), the sweep list, the four parts of a body, and the severity
   ladder. Read it first, because it changes what you go looking for.

3. **Sweep.** Follow the dependency edges the diff touches, not only the lines
   it changes. The most valuable findings a review makes are on files the diff
   never opens — the build script that copies a deleted asset, the test whose
   selector the change invalidates.

4. **Discard.** Read `references/false-positives.md` and drop everything it
   names. A report that spends its first item on a nitpick does not get read
   to its second.

5. **Write findings as JSON**, one object per finding, matching
   `assets/findings.schema.json`. Write the prose here, in `title` and `body`.
   Do not write markdown headings, section names or item numbers — those are
   the renderer's, and text that fights the renderer loses.

6. **Render.** `scripts/render_report.py findings.json --pr <n> ...` produces
   the report. Never assemble the headings by hand: the section a severity
   falls under, the numbering, the truncation rule and the trailer are all
   decided in one place so that a test can read them.

7. **Check.** `scripts/check_report.py <report.md>` re-reads the rendered file
   and fails on anything the contract forbids. Run it on hand-written reports
   too — that is the case it exists for.

## References

Read these on demand, not up front.

| File | Read it when |
|---|---|
| `references/finding-contract.md` | Before reviewing. What is in scope, what to sweep, how to write a title and a body, what each severity means. |
| `references/report-contract.md` | When rendering by hand, or when a check fails and you need the rule. Sections, ordering, numbering, the empty report, the trailer. |
| `references/false-positives.md` | After sweeping, before writing. What not to report. |

## Scripts

`collect_context.py` is stdlib-only: it runs git and nothing else, anywhere
Python and git are.

The other two import `pr_review_agent`, because they call the same
`report.render` and `numbering.assign` the daemon calls — that is what stops
a report written by hand and a report posted by the agent saying the same
findings differently.

You do not have to install the package for that to work. `skill install`
copies the renderer and its import closure into `scripts/_vendor/`, so the
skill is self-contained wherever it lands. If `pr_review_agent` *is*
importable it wins — the vendored copy is on `sys.path` after the
interpreter's own entries, not before — so upgrading the package upgrades
the renderer without reinstalling the skill.

| Script | Does |
|---|---|
| `scripts/collect_context.py` | Header facts from git: head sha, merge base, commit count, changed paths, round number. |
| `scripts/render_report.py` | `findings.json` → the report. `--assign` fills in numbers for new findings against a carried-forward set. |
| `scripts/check_report.py` | Validates a rendered report against `references/report-contract.md`. Exit 1 on violation. |

None of them touch the network, and none of them post anything. Publishing is
a separate, explicit act.

## Two audiences, one contract

This skill is read by a person or an interactive Claude Code session. The
daemon's own reviewer never loads it: `engine/claude.py` runs `claude` with
`--setting-sources ""`, `--restricted` and `--disable-slash-commands`, which
between them mean no skill, plugin or settings file is discovered. That is
deliberate and must not be relaxed to make this skill loadable.

The daemon gets the same text a different way: `engine/prompt.py` reads
`references/finding-contract.md` and `references/false-positives.md` out of
this directory and splices them into the prompt. One source, two deliveries.
If you edit either, you are editing what the daemon tells its reviewer —
`tests/test_skill.py` fails if they fall out of step.

`references/report-contract.md` is the exception, and it is not an oversight:
the daemon never needs to be told the report format, because it never writes
one. It emits findings as JSON and `report.render` lays them out. That
file is for you, for the case where you are rendering or repairing a report
by hand.
