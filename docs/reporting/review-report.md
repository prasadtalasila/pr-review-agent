# Review report template

The shape `publisher.render` produces. Derived from the hand-written
reference report. See issue #45.

This is a *rendering* contract, not a prompt. The reviewer supplies `title`, `body`,
`severity`, `path`, `line` and an optional carried-forward `number`; everything below —
headings, ordering, numbering, the trailer — is decided here, where a test can read it.

---

## Skeleton

```markdown
## Review: PR #<number> — round <r> (`<sha7>`, <c> commits)

## Blocking

<n>. **<title>** <body, wrapped, ending in the remedy paragraph.>

## Should fix

<n>. **<title>** <body.>

## Nits

<prose paragraph, no numbering, no bullets.>

<sub>Automated review. It takes no action on this pull request beyond this comment.</sub>
```

## Rules

**Header.** Always present, always names the head sha, because the comment is edited in
place and a reader must be able to tell which revision the text describes. `round` counts
recorded runs for this pull request; `<c> commits` is `git rev-list --count
<merge_base>..<head>`.

**Sections.** Only non-empty sections are emitted. Order is fixed: Blocking, Should fix,
Nits. Severity maps:

| `Severity` | section |
|---|---|
| `blocker` | Blocking |
| `major`, `minor` | Should fix |
| `nit` | Nits |

The `Severity` enum is not changed by this template. A heading is a rendering decision;
the stored value is data. A `major` that is not a blocker is not printed under a heading
claiming it blocks.

**Numbering.** One sequence across the whole report, not per section — so Blocking may run
1–4 and Should fix start at 8. Numbers are stable for the lifetime of the pull request: a
finding that persists across rounds keeps its number, and a finding that gets fixed leaves
its number vacant. The gaps are information. Never renumber to close them.

**Ordering within a section.** Carried-forward findings in ascending number, then new
findings by `(path, line)`. Stable ordering is what makes re-review an edit-in-place with a
no-op diff when nothing changed.

**Nits are prose.** One paragraph, several small observations joined by sentences. A nit
that deserves a numbered entry is not a nit.

**Empty report.** `## Review: PR #<n> — round <r> (\`<sha7>\`, <c> commits)`, then
`No issues found.`, then the trailer. Round and commit count still appear: "round 3 found
nothing" and "round 1 found nothing" are different statements.

**Trailer.** Unchanged, verbatim, on every comment including the empty one.

## Worked example

Two findings from the reference report, rendered through this template.

```markdown
## Review: PR #1765 — round 3 (`d61de17`, 3 commits)

## Blocking

2. **`script/docs.sh` copies an asset this PR deletes, so the docs build breaks.**
   Line 46 still copies `docs/assets/dtaas-logo-with-text.png`, which `251170e`
   removes. The landing page beside it now references
   `assets/brand/dtaas-logo-full.svg`, and nothing copies that into `site/assets`
   either, so the published redirect page loses its image on both paths.

   Update the publish path in the same commit that moves the assets.

## Should fix

9. **The generators assume they are run from the repo root.**
   `build_brand.py` writes to `pathlib.Path('docs/assets/brand')`, so running it
   from its own directory silently creates a wrong tree. They also sit outside
   whatever `.pylintrc` currently covers.

   Either wire them into the project's Python checks, or mark them as one-shot
   generators in the brand README and resolve the output path relative to
   `__file__`.

## Nits

`BrandMark` uses fixed `clipPath` ids (`brand-mark-built`, `brand-mark-drawn`) while
the generated SVGs use bare `built`/`drawn`; two marks in one document collide.
Identical geometry hides it today — `useId()` plus a per-file prefix in the generator
would remove the trap. The brand README also points a favicon at `png/dtaas-mark-32.png`
while both mkdocs configs use the SVG.

<sub>Automated review. It takes no action on this pull request beyond this comment.</sub>
```

Note what the numbering says without a word of explanation: items 1, 3 and 4 are still
open elsewhere in the report, items 5–8 were raised in rounds 1–2 and fixed.
