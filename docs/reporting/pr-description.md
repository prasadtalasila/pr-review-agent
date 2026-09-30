# Description skill

`pr-review-agent skill install` places two skills. `review-report` is the
[review skill](review-skill.md); **`pr-description`** writes a pull request
description in the shape `@claude describe` posts
([DESCRIBE.md](../DESCRIBE.md)), by the same renderer.

```bash
pr-review-agent skill install          # or --dir <repo>/.claude/skills
```

## 📁 What is in it

| File | Contract |
| --- | --- |
| `SKILL.md` | The workflow: collect context, read the merge-base diff, write `description.json`, render, check. |
| `references/description-contract.md` | What each field holds and what never goes in. **The daemon's describe prompt is this file**, spliced in by `engine/describe.py`; `tests/test_skill_describe.py` fails if the two differ. |
| `references/layout-contract.md` | The rendered layout as named rules — **header**, **type**, **headings**, **table**, **trailer**, **length** — each the identifier `check_description.py` prints. **describe-not-review** is a sentence, not a shape, and is left to the reader. |
| `assets/description.schema.json` | Equal to the `DESCRIPTION_SCHEMA` the engine holds `claude` to. |
| `assets/description.example.json`, `.md` | The worked example; the renderer reproduces the `.md` byte for byte. |
| `scripts/collect_context.py` | A byte copy of the review skill's, so each skill works installed alone. |
| `scripts/render_description.py` | `description.json` → the description, via `description.render_description`. |
| `scripts/check_description.py` | A rendered description against the layout contract. Exit 1 on violation. |

## 🧾 The fields

| Field | Holds |
| --- | --- |
| `type` | `bug_fix`, `enhancement`, `refactor`, `documentation`, `tests` or `other` |
| `summary` | One paragraph, at most 3 000 characters |
| `files` | One `{path, change}` per changed file, `change` at most 300 characters, in reading order |
| `testing` | How to check the change works, at most 3 000 characters |

The type list and the walkthrough table follow pr-agent's `/describe`
(MIT); labels, the release-notes section and writing into the pull request
body are left out.

## 🔌 Without the package

Both scripts that render or check import `pr_review_agent`. `skill install`
copies the renderer and its import closure — now including `description.py`
— into each skill's `scripts/_vendor/`, so either skill works on a machine
that does not have the package; an installed package wins over the copy.
Nothing in the skill touches the network or posts anything.
