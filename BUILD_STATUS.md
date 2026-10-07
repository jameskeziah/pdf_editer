# PDF Branding v3.5.0 Build Status

Updated: 2026-10-07. Engine version and default profile are v3.5.0.

## Implemented roadmap

| Area | Implementation |
| --- | --- |
| Classification and fixtures | Class/subject/chapter/material inference and filters; hashed inventory; deterministic representative selection |
| Whole-document planning | Repeated text/vector/raster detection; explicit PageStrategy and protected content geometry |
| Safe rendering | 1:1 default body placement; designed-page preservation; transparent emblems; native flat-background repair; selective textured cleanup |
| Raster branding | Rendered header/footer English OCR, standalone image boundary verification, unsafe-geometry review gate |
| Opt-in cropping | Profile-controlled rebuild, recorded source clip/matrix, transformed QA checks |
| Strong QA | Word retention/position; protected text/visual regions; image-only render comparisons; legacy text/raster checks |
| Publication | Unique same-directory stages/backups; OS lock; PDF/plan/QA transaction; rollback and journal recovery |
| Cache | Full source/output SHA-256; settings/asset-content/engine fingerprint; malformed-record miss; recovery before cached skip |
| Resumable batches | SQLite transitions, attempts, JSON manifest, bounded workers, safe cancellation, explicit failed-job retry |
| Review | Source/plan/final images, contact sheets, relative-link HTML, mandatory issue and low-confidence pages |
| GUI | Persistent settings; filters; live rows/progress/issues; review navigation; cancel/resume/retry; worker generation isolation |
| Performance | Worker benchmark, process-tree RSS samples, throughput, rendered-output equality and source hashes |
| Regression coverage | Synthetic safety/failure tests, real-source references, actual multiprocessing and withdrawn Tk workflow tests |

## Publication and source preservation

- Source PDFs are read without saving changes to them.
- Passed candidates replace production only after PDF, plan, and QA artifacts are ready for commit.
- QA-failed candidates use a separate filename and preserve existing production.
- Report-write/replacement errors restore prior artifacts; failed rollback retains its journal and verified backups.
- Recovery refuses to overwrite a target edited outside the interrupted transaction.
- Image-only/scanned pages receive visual checks and no unsupported 100% text-retention claim. OCR failure or unsafe recognized geometry is a QA error.
- Full source/output hashes and asset-content fingerprints govern cached skips; pending publication recovery cannot be skipped.

## Final validation evidence

All eight implementation stages are built. The validation scope below is the release evidence; full-library rendering remains a separate rollout.

| Gate | Current release evidence |
| --- | --- |
| Full test suite | `PDF_BRANDING_REAL_TESTS=1 python -m pytest -q`: **225 passed, 0 failed, 0 skipped**, 233.08 seconds. [JUnit](tmp/roadmap_validation/tests.xml) |
| Full-library inventory | **803 PDFs / 10,876 pages**; 801 teaching PDFs classified, 2 administrative exclusions, 0 unclassified, 0 unreadable. [Inventory](tmp/regression_inventory/inventory.md) |
| Representative real-PDF matrix | **21 PDFs / 70 pages passed**, covering all 20 class/subject pairs, 7 document families, and observed layout candidates. Source hashes unchanged; all review bundles generated. [Matrix](tmp/roadmap_validation/representatives/matrix.md) |
| Measurement and Motion v3.5.0 | **7 completed / 70 pages**, including the previously failing 20-page Race PDF. Published to `v3_output_350`; all 7 output hashes/cache records and review links verified. [Manifest](v3_output_350/batch_manifest.json) |
| Visual review | Sampled current contact sheets for designed/scanned artwork, notes, NCERT, framed worksheets, dense maths, diagrams and tables. Optics page 3 logo and background corrected. Independent Poppler Key Points page 1 comparison: **0 changed body pixels outside approved regions**. [Evidence](tmp/roadmap_validation/poppler/verification.json) |
| Worker benchmark | Same 21 PDFs / 70 pages: 1 worker **154.553 s / 422.59 MB RSS**; 2 workers **106.844 s / 820.35 MB**; 4 workers **101.684 s / 1148.96 MB**. All output page pixels/dimensions identical; source hashes unchanged. Recommend **1 worker** with the measured 1049.68 MB available-memory reserve. [Benchmark](tmp/roadmap_validation/benchmark/benchmark.md) |
| Cancellation/resume and transaction faults | Included in the passing full suite: real parallel failure isolation, cancellation/resume, cached resume, journal recovery, report/PDF rollback and injected lock/write failures. |
| GUI validation | **11 GUI tests pass**, including real withdrawn Tk -> actual batch -> embedded contact review -> cached resume with unchanged hashes. Visible desktop/manual interaction was not inspected; this is widget/pipeline validation. |
| Full-library branded regeneration | **Not run.** Only the seven reported chapter PDFs were published. All **803 original source SHA-256 hashes remain unchanged**. Full-library rendering and review of its flagged pages remain rollout work. |

## Acceptance limits

QA checks configured academic content, geometry, rendering, and legacy branding; it does not prove educational answers correct. Header/footer OCR recognizes configured words in limited rendered bands. Diagram/table inventory flags identify review candidates. Contact sheets select representative pages plus every issue/low-confidence page; they do not claim every library page received manual review.

Cropping stays opt-in. Some original footers are intentionally preserved where their replacement would overlap protected content; those pages are highlighted for review. Native OCR emitted tiny clipped-glyph messages during some runs; the completed jobs had no QA errors. The automated checks and sampled visual review do not claim manual inspection of every library page.

[Machine-readable validation summary](tmp/roadmap_validation/validation_summary.json) records source integrity, tests, benchmark, representative scope and published chapter hashes. Historical v3.4.6.post1 chapter evidence remains separate; it is not v3.5.0 whole-library certification.

## Reproducible commands

See [README.md](README.md) for installation, GUI, batch, inventory, matrix, cancellation/resume, and benchmark commands. Use dedicated validation output folders for worker comparisons and fault recovery. Retain fixture hashes and machine-readable reports with final evidence.
