# PDF Branding V3.4.6 — Strategy-Driven Safe Renderer

V3.4.6 uses a strict pipeline:

**discover → metadata → whole-document analysis → PagePlan → render → QA → atomic publish**

The renderer does not guess what to erase. Every destructive action must be declared by the analyzer in the page's plan first.

## What V3.4.6 adds

### Transparent logo replacement without a visible box

The PNG assets already contain a true alpha channel. The visible rectangle seen on dark Key Points pages came from the *legacy-logo cleanup fill*, not from the logo PNG itself. V3.4.6 fixes that by:

- estimating the dominant local background colour around the old logo;
- **not** redacting the entire designed-page logo rectangle;
- selectively masking only the bright/dark legacy-logo foreground pixels, preserving gradients and textures underneath;
- tightly redacting searchable legacy text only where it actually occurs;
- then placing the transparent school emblem over the preserved artwork.

This keeps the old ALLEN artwork removed while avoiding the grey/brown boxed-sticker effect.

### Explicit page strategies

Every page now receives one semantic strategy:

```text
STANDARD_HEADER_FOOTER_REPLACEMENT
FIRST_PAGE_LARGE_LOGO_REPLACEMENT
DESIGNED_PAGE_PRESERVE
DESIGNED_PAGE_LOGO_ONLY
LEGACY_ONLY_CLEANUP
LEAVE_UNTOUCHED
```

This is separate from the low-level render method (`overlay`, `preserve`, etc.). It makes the plan report explain *why* a page is being modified.

### Designed chapter pages are protected

An illustrated chapter-opening Notes page is classified as a designed page when it contains the chapter title, has substantial imagery, and has no verified legacy top logo. V3.4.6 then forbids the normal SSKEMS header on that page.

Designed pages may still receive:

- verified ALLEN logo-only replacement,
- vertical publisher/module-text cleanup,
- verified in-place footer replacement.

The academic artwork remains at 1:1 coordinates.

### Stronger footer detection

Repeated footer analysis combines:

- repeated text signatures,
- vector horizontal rules / filled bands,
- low-resolution raster band analysis.

The raster detector is specifically for flattened/light-blue source page-number bands that are not exposed as simple PDF drawing rectangles.

When a repeated old footer is verified, V3.4.6 redacts the complete old footer region and then draws the SSKEMS footer inside the same area. It does not append a second footer below it.

### Adaptive header footprint

Normal dynamic header target: `54 pt`  
Maximum adaptive header footprint: `76 pt`

The old-brand cleanup band and the visible header are still independent. A large first-page ALLEN logo can require a deeper cleanup than the visible SSKEMS header, while the cleanup remains hard-bounded before the first verified academic body content.

### QA gates

QA checks include:

- page count,
- page dimensions,
- body-text retention,
- body-coordinate shift,
- searchable ALLEN remnants,
- remaining vertical legacy text,
- generic header accidentally assigned to a designed page,
- high-confidence repeated footer without an in-place replacement plan.

A failed file is saved as `*.qa_failed.pdf`; it never silently becomes the production output.

## Local correction: V3.4.6.post1

- QA compares individual PDF words using the same extraction on both sides. Superscripts such as `10th` and `19th`, and words spanning font changes, no longer produce false text-loss errors.
- Header, footer, logo, and approved cleanup regions are excluded from both sides, so new branding cannot hide missing academic text. Body words in a block that also contains a header are still checked individually. The 98.5% retention threshold is unchanged.
- A batch containing `qa_failed` now exits with code `2`, matching processing failures.
- Flat designed-page logo repairs reuse a verified blank portion of the source PDF background, preserving its color space and transparency across PDF viewers. Textured backgrounds retain the selective repair path.
- The engine cache version changes to `3.4.6.post1`; old cached QA results must be verified again.

## Install on Windows

Open PowerShell inside the extracted `pdf_branding_v3_4_6` folder:

```powershell
py -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Verify:

```powershell
python -m pytest -q
```

Expected:

```text
48 passed
```

## Recommended next step: plans only

Place or copy your existing `foundation_notes` folder into this project, then run:

```powershell
python -m pdf_branding.batch `
".\foundation_notes" `
".\v3_output" `
--class-filter 6 `
--subject-filter physics `
--chapter-filter "Measurement and Motion" `
--analyze-only `
--preview-plans
```

Nothing is modified in analyze-only mode.

Inspect:

```text
v3_output\_reports\...\*.plan.json
v3_output\_plan_previews\...\page_###_plan.png
```

Preview colours:

- **Red** = verified legacy cleanup
- **Green** = new dynamic header footprint
- **Blue** = in-place footer replacement
- **Purple** = logo-only replacement on designed pages
- **Orange** = vertical publisher/module-text cleanup

The bottom preview label also shows the semantic V3.4.6 page strategy.

## What to approve before rendering

For Measurement and Motion, check at least:

- illustrated chapter-opening Notes page → `DESIGNED_PAGE_PRESERVE`, no green generic header;
- Key Points / dark designed page → `DESIGNED_PAGE_LOGO_ONLY` when a logo is verified;
- Test first page → `FIRST_PAGE_LARGE_LOGO_REPLACEMENT`;
- Test later pages → standard top-strip replacement;
- NCERT / Exercise / DPP pages → repeated source header replaced without touching the first question;
- old blue page-number strips → blue footer replacement rectangle should cover the source strip;
- vertical publisher text → orange rectangle should cover it without touching body content.

Only after those previews look correct should you run the renderer:

```powershell
python -m pdf_branding.batch `
".\foundation_notes" `
".\v3_output" `
--class-filter 6 `
--subject-filter physics `
--chapter-filter "Measurement and Motion" `
--overwrite
```

## Architecture status

1. DocumentProfile + PagePlan — implemented
2. analysis separated from rendering — implemented
3. whole-document repeated header/footer detection — implemented
4. automatic crop recommendation — implemented, disabled by default
5. output QA + legacy detection — implemented
6. atomic saves + cleanup — implemented
7. caching/performance + optional workers — implemented
8. JSON branding profiles — implemented
9. regression suite — **48 passing tests**
10. GUI — implemented

The remaining gate is real-library visual approval, not more blind redaction rules.


## V3.4.6 designed-page refinement

V3.4.6 strengthens first-page chapter-opener recognition. A Notes page is treated as a designed opener when it has a prominent chapter title near the top, visual/layout signals, and does not itself carry the recurring structural source header used on later pages. This works even when the document as a whole has repeated NCERT/ALLEN headers.

Designed pages now:
- never receive the generic SSKEMS header;
- never receive the generic dynamic footer;
- remain at 1:1 geometry;
- can still remove verified vertical publisher text;
- use logo-only replacement when a verified legacy logo is present.

## V3.4.6 designed-page margin cleanup

V3.4.6 keeps the designed-page preserve/logo-only strategies from V3.4.1 and strengthens verified side-margin cleanup. Some source PDFs expose the vertical PNCF/LIVE Module publisher strip as a tall narrow text block without reliable rotation metadata. V3.4.6 now detects that case conservatively only when the block is in a side margin, has strongly vertical geometry, and matches configured legacy side-text terms. The designed artwork, chapter title, and body remain 1:1; no generic header or footer is inserted on designed pages.

## V3.4.6: faint ALLEN ghost removal

V3.4.4 removed bright legacy pixels correctly but could leave a faint dark-blue/black ALLEN outline on a dark designed background. V3.4.6 no longer assumes the old logo is brighter than the page. It uses adaptive bidirectional colour-distance cleanup, then expands the detected mask slightly to remove antialiased fringes. This applies to designed Key Points pages without flattening the complete replacement rectangle.


## V3.4.6: complete ALLEN shadow removal on flat designed backgrounds

V3.4.5 could still leave a faint low-contrast ALLEN shadow after the bright logo pixels were removed. V3.4.6 adds a two-stage repair:

- if the legacy logo sits on a locally flat/background-dominated field, rebuild the protected interior of the logo zone from the sampled local background and then place the transparent school emblem;
- protect an outer ~2.6 pt frame so nearby page rules / lane separators survive;
- feather the repair edge to avoid a visible rectangular seam;
- if the region is genuinely textured, fall back to the selective pixel-mask cleanup instead of flattening artwork.

This removes both the bright ALLEN letters and the faint dark shadow/antialiasing visible on the Key Points pages.
