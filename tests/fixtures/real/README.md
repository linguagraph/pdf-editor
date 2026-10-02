# Real-world PDF corpus

Put curated real-world PDFs here (scanned, signed, forms, unusual producers, broken files).
For each file, add a row below with its source and license. Only commit files whose license
allows redistribution; otherwise keep them local (the tests skip missing files). Keep the whole
folder small (under ~5 MB). Tests: `tests/services/test_real_corpus.py`.

| File | What it exercises | Source | License |
|------|-------------------|--------|---------|
| `irs_fw9.pdf` (141 KB, 6 pages) | Real fillable AcroForm (23 widgets on page 1: text fields, checkboxes) with XFA data, tagged PDF (`/StructTreeRoot`, `/Lang`), JavaScript, a `/Perms /UR3` usage-rights signature and `SigFlags` AppendOnly; `/Lang` and `/DisplayDocTitle` are stored as indirect objects. Produced by Adobe LiveCycle Designer 6.5. | IRS Form W-9 (Rev. March 2024), <https://www.irs.gov/pub/irs-pdf/fw9.pdf>, downloaded 2026-09-27 | Public domain: work of the U.S. federal government (17 U.S.C. § 105) |
| `scan_navy_letter_1953.pdf` (129 KB, 1 page) | Scanned typewritten letter with no text layer (JPEG page image plus mask-compressed overlays from a scanner, "Quartz PDFContext" producer): rendering, OCR | Wikimedia Commons, [File:US Navy Letter of Commendation 19531215.pdf](https://commons.wikimedia.org/wiki/File:US_Navy_Letter_of_Commendation_19531215.pdf) (<https://upload.wikimedia.org/wikipedia/commons/4/42/US_Navy_Letter_of_Commendation_19531215.pdf>) | Public domain: work of a U.S. Navy employee in official duties (PD-USGov-Military-Navy; Commons tags it PD-scan / Public Domain Mark 1.0) |
| `signed_selfsigned.pdf` (9 KB, 1 page) | Digitally signed PDF (PAdES-style CMS signature by pyHanko, `SigFlags` 3): the signature's byte range must survive an incremental save | Generated once by `scripts/make_signed_sample.py` (pyHanko, dev-only dependency) with a throwaway **self-signed test certificate** ("PDF Editor Test Signer"; the private key was never stored). Not a real-world signer: no redistributable real signed PDF was found | Made for this project; same license as the repository (AGPL-3.0-or-later) |

Local-only samples (git-ignored; tests that use them skip when they're missing):

- `scanned_dryer_manual.pdf`: one scanned page of an appliance manual (bulleted text, headings,
  a figure with its own labels, drawn through nested form XObjects). Used by
  `tests/services/test_ocr_editable.py` for editable OCR output. A third-party manual, so it
  isn't committed.

To replace the signed sample, run `uv run python scripts/make_signed_sample.py` (it needs the
`dev` extra) and commit the new file.
