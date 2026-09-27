"""Download Tesseract language data into src/pdfeditor/data/tessdata (bundled in the exe).

    uv run python scripts/fetch_tessdata.py            # English (the default bundle)
    uv run python scripts/fetch_tessdata.py eng deu    # more languages

Uses the "fast" models (Apache 2.0): small and accurate enough for scanned documents.
The files are not committed; CI and the build script fetch them.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
OUT = Path(__file__).resolve().parent.parent / "src" / "pdfeditor" / "data" / "tessdata"


def fetch(languages: list[str]) -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    for lang in languages:
        target = OUT / f"{lang}.traineddata"
        if target.exists() and target.stat().st_size > 100_000:
            print(f"{target.name}: already present")
            written.append(target)
            continue
        tmp = target.with_suffix(".part")
        with urllib.request.urlopen(URL.format(lang=lang), timeout=120) as response:
            tmp.write_bytes(response.read())
        tmp.replace(target)
        print(f"{target.name}: {target.stat().st_size // 1024} KB")
        written.append(target)
    return written


if __name__ == "__main__":
    fetch(sys.argv[1:] or ["eng"])
