# PDF Branding v3.5.0

PDF Branding processes school PDFs through a planned pipeline:

**discover -> classify -> analyze -> PagePlan -> render -> QA -> publish**

Source PDFs stay unchanged. Each page plan declares permitted cleanup, protected academic text and artwork, page strategy, and any source-to-output transform. Outputs that fail QA use `*.qa_failed.pdf`; they do not replace an existing production PDF.

## Implemented features

- Class, subject, chapter, and material filters, plus a hashed real-library inventory and representative regression matrix.
- Whole-document header/footer detection using text, vector rules, and raster footer bands.
- Protected text, diagrams, tables, and images. Illustrated chapter openers and Key Points pages retain their designed layout and suppress generic headers/footers.
- Transparent school emblems, selective textured-background cleanup, and native PDF background repair for verified flat logo fields, preserving color space and transparency.
- Header/footer OCR with the bundled English model for configured legacy words in raster branding bands. Unsafe recognized boundaries or OCR failures are held for QA review.
- QA for page count/size, body words and positions, protected geometry, rendered body/visual changes, and legacy text/raster remnants. Image-only pages receive rendered-content checks rather than an automatic text-retention pass.
- Recoverable PDF/plan/QA publication, full-content cache checks, SQLite checkpoints, bounded worker scheduling, cancellation, and processing-error retries.
- A persistent Tkinter GUI with filters, live status, contact-sheet review, cancel, resume, and retry controls.
- A benchmark measuring serial/parallel throughput, process-tree memory, output pixel equality, and source hashes.

The default renderer preserves body coordinates. Crop rebuilding is opt-in through `layout.allow_rebuild_crop` in a JSON profile; a safe plan and explicit transform are required, and QA checks the mapped result.

## Install and start

From PowerShell in this project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
```

If PowerShell blocks activation, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal, then activate again. Tkinter must be available in the Python installation for the GUI.

Start the GUI with:

```powershell
python -m pdf_branding.gui
```

`Start_GUI.bat` launches the same application. GUI settings persist under `%LOCALAPPDATA%\PDFBranding\gui_settings.json`.

Choose the source folder, output folder, and profile, then use **Classify**, **Analyze + review**, or **Process + QA**. The results table shows status, family, attempts, and elapsed time. Select a row to inspect its contact sheets or open its PDF/review. **Cancel** stops new jobs while active documents finish safely; **Resume** processes remaining jobs and verifies completed outputs before skipping them.

## CLI workflow

Classify a selected chapter without producing branded PDFs:

```powershell
python -m pdf_branding.batch ".\foundation_notes" ".\v3_output_350" `
  --class-filter 6 --subject-filter physics `
  --chapter-filter "Measurement and Motion" --dry-run
```

Generate plans and source/plan review images:

```powershell
python -m pdf_branding.batch ".\foundation_notes" ".\v3_output_350" `
  --class-filter 6 --subject-filter physics `
  --chapter-filter "Measurement and Motion" `
  --analyze-only --preview-plans --contact-sheets
```

Analyze-only writes reports and previews; it leaves source and branded output PDFs unchanged. Review colors are red for cleanup, green for a header, blue for a footer, purple for a logo, and orange for side text. Reviews include all QA-issue and low-confidence pages, even when these exceed the representative-page budget.

Process the same chapter:

```powershell
python -m pdf_branding.batch ".\foundation_notes" ".\v3_output_350" `
  --class-filter 6 --subject-filter physics `
  --chapter-filter "Measurement and Motion" --workers 2 --contact-sheets
```

Use `--overwrite` to regenerate existing outputs and `--profile ".\profiles\custom.json"` for another validated profile. `--material-filter`, `--limit`, and `--show-skipped` narrow or explain a run.

Resume or explicitly retry unsuccessful jobs:

```powershell
python -m pdf_branding.batch ".\foundation_notes" ".\v3_output_350" --resume --workers 2
python -m pdf_branding.batch ".\foundation_notes" ".\v3_output_350" --retry-failed --workers 2
```

`--retries 2` retries processing errors up to twice; QA failures are not retried automatically. `--retry-failed` explicitly selects failed, QA-failed, interrupted, and cancelled jobs. `--no-cache` forces processing. With `--resume`, verified completed outputs may be skipped even when `--overwrite` is also present.

For cancellation from another terminal, pass `--cancel-file ".\stop-branding.txt"`; creating that file stops scheduling. Remove it before resuming. Workers range from 1 to 8, capped by CPU count. Active work is bounded by the worker count.

Batch exit codes are `0` for success, `2` when a job failed processing or QA, and `130` for requested cancellation.

## Reports, cache, and recovery

| Output artifact | Purpose |
| --- | --- |
| Original relative path and PDF filename | QA-passed production output |
| `*.qa_failed.pdf` | Candidate held for inspection after QA failure |
| `_reports/<relative folder>/<stem>.plan.json` | Declared strategy, geometry, and cleanup |
| `_reports/<relative folder>/<stem>.qa.json` | QA metrics, issue codes, and page numbers |
| `_reviews/<relative folder>/<stem>/index.html` | Source/plan/final contact-sheet review |
| `_plan_previews/.../page_###_plan.png` | Annotated plans when requested |
| `batch_report.json` | Records from the current invocation |
| `batch_manifest.json` | Durable job history exported for review and GUI reload |
| `.batch_jobs.sqlite3` | SQLite job transitions and attempts |
| `.pdf_branding_cache.json` | Source/output hashes and passed-QA cache records |

PDFs and their plan/QA reports publish through a journaled transaction. Files are staged beside their final destinations before replacement; reports publish before the PDF. Write/replacement errors restore prior final files. An interrupted prepared transaction rolls back when that document is processed again. Transaction backups are removed after a successful commit.

Keep the output database and any `*.publication.json` recovery journals when resuming. OS locks release after a process exits; persistent lock-file names alone do not indicate active work. A pending publication journal prevents a cached skip.

A cache hit requires matching source/output SHA-256, profile settings and configured asset bytes, engine version, and a prior passed-QA record. Same-size corruption, changed logos, and stale engine versions trigger processing again. Profiles validate structure, numeric ranges, colors, and assets before processing.

## Inventory, regression matrix, and benchmark

Build a read-only library inventory with classification, layout flags, hashes, and representative selection:

```powershell
python -m pdf_branding.regression inventory ".\foundation_notes" ".\validation\inventory"
```

Render a representative regression matrix with QA and review bundles:

```powershell
python -m pdf_branding.regression matrix ".\foundation_notes" ".\validation\matrix" `
  --manifest ".\validation\inventory\inventory.json"
```

Diagram/table flags are review candidates derived from geometry. Missing real-source layouts are recorded explicitly; synthetic coverage stays distinct from real-library validation.

Measure worker counts against the same selected fixtures:

```powershell
python -m pdf_branding.benchmark ".\foundation_notes" ".\validation\benchmark" `
  --manifest ".\tests\fixtures\real_matrix.json" --workers 1 2 4
```

The benchmark records throughput, sampled process-tree RSS, and each page's rendered pixel signature. Worker recommendations use measured successful runs; QA failures remain failures. `--selection-manifest` on the batch command can process fixture lists or inventory representatives.

Bundled OCR provenance and hashes are in [assets/ocr/README.md](assets/ocr/README.md). Header/footer OCR is limited to configured legacy words and cannot certify arbitrary branding elsewhere on a page.

## Release validation

Implementation and evidence are tracked in [BUILD_STATUS.md](BUILD_STATUS.md).

**v3.5.0:** 225 tests pass; 21 real representative PDFs and all 7 Measurement and Motion PDFs pass QA. The inventory accounts for all 803 files (801 teaching PDFs, 2 administrative exclusions, no unclassified files); all original source hashes remain unchanged.

Corrected chapter outputs and contact reviews are in `v3_output_350`. One worker is recommended on the measured machine; cropping remains opt-in. Full-library rendering and review of its flagged pages remain rollout work. See [BUILD_STATUS.md](BUILD_STATUS.md) for exact scope, timings, memory, GUI evidence and limits.
