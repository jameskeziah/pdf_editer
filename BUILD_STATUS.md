# PDF Branding V3.4.6 Build Status

V3.4.6 adds background-matched transparent logo replacement on top of the strategy-driven V3 architecture.

## New in V3.4.6

- Explicit `PageStrategy` contract:
  - `STANDARD_HEADER_FOOTER_REPLACEMENT`
  - `FIRST_PAGE_LARGE_LOGO_REPLACEMENT`
  - `DESIGNED_PAGE_PRESERVE`
  - `DESIGNED_PAGE_LOGO_ONLY`
  - `LEGACY_ONLY_CLEANUP`
  - `LEAVE_UNTOUCHED`
- Designed chapter-opening detection: an illustrated Notes first page with the chapter title and no verified legacy top logo is preserved 1:1 and cannot receive the generic dynamic header.
- Repeated footer detection now includes raster analysis, so flattened/light-blue page-number strips can be detected even when `get_drawings()` exposes no useful rectangle.
- Verified old footer regions are truly redacted before the SSKEMS footer is drawn; they are not merely covered visually.
- Dynamic header footprint can adapt from the normal 54 pt up to a capped 76 pt when a deeper verified legacy region is available.
- Designed pages can still receive verified side-text cleanup and in-place footer replacement without disturbing the main artwork.
- QA fails if a designed page is assigned a generic header or if a high-confidence repeated footer exists but no replacement plan was generated.

## Roadmap status

| Roadmap item | Status |
|---|---|
| DocumentProfile + PagePlan | Implemented |
| Separate analysis / rendering | Implemented |
| Whole-document repeated header/footer detection | Implemented: text + vector rules + raster footer bands |
| Automatic crop calculation | Implemented as recommendation; disabled by default |
| Output QA + legacy detection | Implemented |
| Atomic saves + exception-safe cleanup | Implemented |
| Batch caching + performance | Implemented |
| External JSON branding profiles | Implemented |
| Regression-test suite | **48 tests passing** |
| GUI | Implemented |

## Release gate

**Architecture:** READY  
**Synthetic regression suite:** PASS (48/48)  
**Representative chapter:** VERIFIED — Measurement and Motion, Class 6 Physics: 7/7 outputs pass QA; all 70 pages reviewed.  
**Mass library render:** NOT RUN — the remaining library has not been regenerated with this correction.


### V3.4.6 refinement
- Prominent chapter-title detection added.
- Designed opener decision is page-local, not defeated by repeated headers found only on later pages.
- Designed pages suppress generic header and footer.
- QA exempts intentionally preserved designed pages from the repeated-footer replacement requirement.

### Transparent-logo repair
- Both school emblem PNG assets are verified RGBA with fully transparent corners.
- The old visible box was caused by an inaccurate legacy-logo cleanup fill, not by missing PNG transparency.
- V3.4.6 no longer clears the whole designed-page logo rectangle. It performs selective foreground-pixel cleanup, leaving the surrounding artwork/gradient untouched.
- Searchable legacy text is redacted only at its tight hit box; raster/vector legacy marks are masked selectively.
- Regression tests verify alpha transparency, flat-background matching, and non-flattening on textured/gradient backgrounds.

### V3.4.6 faint legacy-logo ghost repair
- Designed-page cleanup is now bidirectional: it removes legacy foreground that is either lighter **or darker** than the sampled local background.
- The pixel threshold adapts to local edge noise/gradient but is capped so dark-blue/black ALLEN antialiasing cannot survive as a faint ghost.
- A wider protected outer frame preserves page borders and lane separators touching the fallback logo rectangle.
- The cleanup mask grows slightly after detection to remove antialiased outlines before the transparent school emblem is placed.


### V3.4.6 complete legacy-logo cleanup
- Real-file inspection confirmed a low-contrast ALLEN shadow could remain behind the transparent school emblem.
- Flat designed logo zones now use a protected, feathered full-background rebuild instead of relying only on foreground thresholding.
- Textured / non-uniform artwork still uses selective cleanup.
- Added a regression fixture using the sampled residual shadow colour `(66, 53, 42)` on the brown Key Points background `(71, 51, 26)`.
- Regression suite: **48 passed**.

### V3.4.6.post1 real-library correction (2026-10-07)

- Race pages 19-20 failed because QA tokenized superscript ordinals differently between source and output. Shared word extraction fixes this without lowering the 98.5% threshold.
- Word-level region checks retain academic words in mixed header/body blocks and prevent inserted branding from hiding true body-text loss.
- QA-failed batches now exit with code `2`.
- Flat Key Points logo repairs reuse verified blank native PDF artwork, preserving the source color space/transparency instead of baking a viewer-dependent RGB patch. Textured regions keep the selective fallback.
- Regenerated the seven selected outputs: **7 completed, 0 QA failures**, with **100% checked body-word retention** and unchanged source SHA-256 hashes.
- Rendering comparison: 64 pages match their previous render exactly; six Key Points pages change only within their logo regions. Poppler and MuPDF both show matching page-one repair/background colors.
- Original outputs, reports, and changed source files are retained in `_backups/measurement-motion-20261007-qa-fix/`. The machine-readable audit is `v3_output_346/_reports/measurement_motion_validation.json`.
