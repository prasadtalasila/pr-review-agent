# Describe and review verbs

An allowlisted maintainer can ask for one of two things in a comment:

```text
@claude review      a review, as before
@claude describe    a pull request description, posted as a comment
```

Implemented in `triggers/classifier.py` (`command_of`),
`engine/describe.py`, `description.py` and the
[`pr-description` skill](reporting/pr-description.md). Issue
[#128](https://github.com/prasadtalasila/pr-review-agent/issues/128).

## 🔤 How the verb is read

The word straight after the **first** `@claude` in the comment's prose, on
the same line, case-insensitive. Two words mean something; everything else
is a review, which is what `@claude` meant before verbs existed.

| Comment by an allowlisted user | Result |
| --- | --- |
| `@claude` | review |
| `@claude review` | review |
| `@claude describe` | description |
| `@Claude DESCRIBE this please` | description |
| `@claude please take a look` | review |
| `@claude improve` | review — there is no `improve` |
| `@claude review, and @claude describe` | review — only the first mention is read |
| `` `@claude describe` `` or `> @claude describe` | nothing — not a mention at all |

**The verb picks one of two outputs and carries no argument.** Nothing after
it is read, so a comment cannot use it to reach the prompt, the budget or
what the publisher writes. Who may ask is unchanged: the commenter's numeric
user id against the allowlist, never the login. A new pull request is always
a review.

The mention's dedupe key is the comment id, as before, so editing an
existing `@claude` comment into `@claude describe` asks nothing new. Post a
fresh comment.

## 📝 What a description is

```markdown
## Description: PR #130 (`6a6d208`, 4 commits)

**Type:** Enhancement

<one paragraph: what the change does, and what a reader must know before merging>

## Changes

| File | Change |
| :-- | :-- |
| `src/.../since.py` | <one sentence> |

## How to test

<the commands to run, and what to look for>

<sub>Automated description. It takes no action on this pull request beyond this comment; copy what is useful into the description.</sub>
```

The contract the engine is given is
[`description-contract.md`](reporting/pr-description.md), read from the
packaged skill so the daemon and an interactive session write the same
thing. Every field goes through `sanitise` before it is posted, paths are
code spans, and a pull request too wide for one comment loses table rows
from the end, counted in one line, rather than its summary or test plan.

## 🔒 What does not change

**The write set.** A description is one ordinary issue comment, exactly as a
review is. It is not written into the pull request body: editing text a
contributor wrote is a different kind of write, and it loses their text when
the model is wrong. `tests/test_publisher_describe.py` pins that one post to
`/issues/{n}/comments` carrying only a `body` is all it does.

**The sandbox.** The `claude` argv differs from a review's in the schema and
the system prompt and nothing else — the read-only tool set, `--restricted`,
the settings isolation and the permission flags are the same elements, which
`tests/test_cli_engine_describe.py` asserts element by element.

**The spending rails.** This is the change CLAUDE.md §5 asks to be named: a
comment can now make the agent spend on something that is not a review. It
goes through every rail a review does, and `tests/test_worker_describe.py`
pins each one:

- the governor reserves before the engine runs and settles after, on the
  same ledger;
- `budget.enabled: false` refuses it;
- it counts against `budget.max_reviews_per_pull_request` and the pacer's
  interval, because both count ledger rows — so a describe is one of the
  day's reviews, not a way around the cap;
- the pre-flight estimate prices it as a review of the same diff, which
  errs high.

## 🧮 What is different about the run

- **Always the whole change.** A description is never incremental and is
  shown no earlier findings.
- **Not a round.** It is recorded in `runs` so a failed post can be retried
  without paying again, but the cross-round reads — the last round's
  findings, the round number, where an incremental diff starts, the finding
  numbers — skip it. The review after a description is still round 2.
- **Not a sample for the estimate.** It settles with `reviewed_lines` NULL,
  so the fit never reads it (an incremental round is set aside differently,
  by `reviewed_since`): its output is a fraction of a review's, and
  fitting it would pull the estimate below a review's price — the direction
  that under-refuses.
- **Folded only with its own kind.** Several `@claude describe` comments
  waiting on one pull request are answered by one description, as mentions
  are by one review; a waiting `@claude review` is not answered by a
  description, nor the other way round.
- **No standards files.** They say how to review a change, not what it does.
