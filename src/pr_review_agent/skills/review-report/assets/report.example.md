## Review: PR #1765 — round 3 (`d61de17`, 3 commits)

**Effort** 3/5 · **Risk** medium · **Changes required** · Start with: `script/docs.sh`, `script/build_brand.py`

## Blocking

2. **`script/docs.sh` copies an asset this PR deletes, so the docs build breaks.**

   Line 46 still copies `docs/assets/dtaas-logo-with-text.png`, which `251170e` removes. The landing page beside it now references `assets/brand/dtaas-logo-full.svg`, and nothing copies that into `site/assets` either, so the published redirect page loses its image on both paths.

   Update the publish path in the same commit that moves the assets.

## Should fix

9. **The generators assume they are run from the repo root.**

   `build_brand.py` writes to `pathlib.Path('docs/assets/brand')`, so running it from its own directory silently creates a wrong tree. They also sit outside whatever `.pylintrc` currently covers.

   Either wire them into the project's Python checks, or mark them as one-shot generators in the brand README and resolve the output path relative to `__file__`.

## Nits

`BrandMark` uses fixed `clipPath` ids (`brand-mark-built`, `brand-mark-drawn`) while the generated SVGs use bare `built`/`drawn`; two marks in one document collide. Identical geometry hides it today — `useId()` plus a per-file prefix in the generator would remove the trap. The brand README also points a favicon at `png/dtaas-mark-32.png` while both mkdocs configs use the SVG.

<sub>Automated review. It takes no action on this pull request beyond this comment.</sub>
