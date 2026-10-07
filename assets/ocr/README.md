# Bundled English OCR model

`eng.traineddata` is the official English `tessdata_fast` model distributed by the Tesseract OCR project. This package uses it through PyMuPDF's OCR API for rendered header/footer legacy-brand checks.

## Upstream provenance

- Project: [tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast).
- Model: [eng.traineddata at commit 923915d4ced2a7235221788285785a29c4a42d4a](https://github.com/tesseract-ocr/tessdata_fast/blob/923915d4ced2a7235221788285785a29c4a42d4a/eng.traineddata).
- Pinned model download: [raw eng.traineddata](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/923915d4ced2a7235221788285785a29c4a42d4a/eng.traineddata).
- License source: [LICENSE at commit 27cfc71a8874cce2483679eea010e391bb38c2ae](https://github.com/tesseract-ocr/tessdata_fast/blob/27cfc71a8874cce2483679eea010e391bb38c2ae/LICENSE).
- Verification date: 2026-10-07. The local model and license were compared byte-for-byte with these pinned upstream files; both matched.

The upstream repository describes these as fast integer LSTM models for Tesseract 4/5 and licenses its data under Apache License 2.0. The original license text is included unchanged as [LICENSE](LICENSE). The English model is also included unchanged. The model-file commit predates the separate license-file commit; both sources are recorded explicitly.

## Locally measured files

| File | Size in bytes | SHA-256 |
| --- | ---: | --- |
| `eng.traineddata` | 4113088 | `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2` |
| `LICENSE` | 11358 | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |

Recheck the local copies from the project root:

```powershell
Get-FileHash -LiteralPath ".\assets\ocr\eng.traineddata" -Algorithm SHA256
Get-FileHash -LiteralPath ".\assets\ocr\LICENSE" -Algorithm SHA256
```

## Application use

The default profile resolves `assets.ocr_eng` to `../assets/ocr/eng.traineddata`, relative to the profile directory. Profile validation checks that the configured model exists and has a plausible traineddata structure. The cache profile fingerprint includes the model's complete file contents.

OCR is used for configured legacy words in rendered header/footer bands, including raster-only branding. It is not a full academic-text transcription or a guarantee of detecting every logo elsewhere on a page. If a recognized logo lacks a safely verified replacement boundary, or OCR cannot run, the affected candidate is held for QA review.
