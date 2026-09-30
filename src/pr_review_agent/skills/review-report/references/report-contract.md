# The report contract

What `report.render` produces, stated as rules so that a hand-written
report can be checked against the same ones. Where a rule is named below
in **bold**, that is the identifier `scripts/check_report.py` prints when the
rule is broken. It checks all of them but one: **no-verdict** is about
sentences rather than shape, and a checker that matched for it would fire on
a finding that quotes one.

## Skeleton

```markdown
## Review: PR #<number> — round <r> (`<sha7>`, <c> commits)

**Effort** <e>/5 · **Risk** <risk> · **<Recommendation>** · Start with: `<path>`, `<path>`

## Blocking

<n>. **<title>**

   <body, indented three spaces, ending in the remedy paragraph.>

## Should fix

<n>. **<title>**

   <body.>

## Nits

<prose paragraph, no numbering, no bullets.>

<sub>Automated review. It takes no action on this pull request beyond this comment.</sub>
```

## Rules

**header** — always present, always names the head sha, because the comment
is edited in place and a reader must be able to tell which revision the text
describes. `round` counts recorded runs for this pull request; `<c> commits`
is `git rev-list --count <merge_base>..<head>`. `collect_context.py` prints
all four values.

**assessment** — always present, including on the empty report, as the
first line after the header. `effort` is 1–5, `risk` is `low`, `medium` or
`high`, and the recommendation reads **Safe to merge**, **Merge with
caution** or **Changes required**. `Start with:` lists the priority files
as code spans, at most five, and is left off when there are none. The paths
are engine output about the contributor's tree, so they are fenced and
sanitised like the rest of the prose.

**sections** — only non-empty sections are emitted, and the order is fixed:
Blocking, Should fix, Nits. Severity maps:

| severity | section |
|---|---|
| `blocker` | Blocking |
| `major`, `minor` | Should fix |
| `nit` | Nits |

`major` sits under "Should fix" rather than under a heading claiming it
blocks. The heading is a rendering decision; the stored severity is data, and
neither one is derived from the other at read time.

**numbering** — one sequence across the whole report, not per section, so
Blocking may run 1–4 and Should fix start at 8. Numbers are stable for the
lifetime of the pull request: a finding that persists across rounds keeps its
number, and a finding that gets fixed leaves its number vacant. The gaps are
information. Never renumber to close them.

**ordering** — within a section, carried-forward findings in ascending
number, then new findings by `(path, line)`. Stable ordering is what makes a
re-review an edit-in-place with a no-op diff when nothing changed.

**nits-are-prose** — one paragraph, several small observations joined into
sentences. No numbers, no bullets. A nit that deserves a numbered entry is
not a nit; raise its severity instead.

**empty-report** — the header, the assessment line, then `No issues found.`,
then the trailer.
Round and commit count still appear: "round 3 found nothing" and "round 1
found nothing" are different statements.

**trailer** — verbatim, on every report including the empty one:

```html
<sub>Automated review. It takes no action on this pull request beyond this comment.</sub>
```

Not editable, not omittable, and nothing follows it. A reader has to be able
to tell at a glance that the comment is machine-written and inert.

**no-verdict** — the only verdict in a report is the recommendation on the
assessment line, and it is advisory: the comment approves nothing and blocks
nothing. No finding or other text gives an approval or a merge
recommendation, and the report does not summarise what the pull request
does. If the findings list is empty, the report says so and stops.

**length** — `MAX_BODY_CHARS` bounds the comment. Over it, whole sections are
dropped lowest-severity-first and a truncation note is added; only when the
highest-severity section alone is over the limit is prose cut mid-sentence.
The renderer does this; do not pre-trim findings to fit.

## Worked example

`../assets/report.example.md` is the rendered form of
`../assets/findings.example.json`. Read them side by side once — the numbering
in that example carries the whole idea. Items 1, 3 and 4 are open elsewhere in
the report; 5–8 were raised in earlier rounds and fixed. Nothing says so in
words.
