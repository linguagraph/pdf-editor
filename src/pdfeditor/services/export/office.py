"""Office documents to PDF through an installed LibreOffice (optional; never bundled)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

OFFICE_SUFFIXES = (
    ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp", ".txt",
)  # fmt: skip
INSTALL_HINT = (
    "Converting Office files needs LibreOffice (free, libreoffice.org). "
    "Install it and try again; the editor finds it automatically."
)


def find_soffice() -> Path | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found)
    candidates = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        if base := os.environ.get(env):
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    candidates += [
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        Path("/usr/bin/soffice"),
        Path("/usr/lib/libreoffice/program/soffice"),
    ]
    return next((c for c in candidates if c.exists()), None)


def convert_to_pdf(source: Path, timeout: float = 180) -> bytes:
    """Convert with ``soffice --headless --convert-to pdf``; raises RuntimeError on failure."""
    soffice = find_soffice()
    if soffice is None:
        raise RuntimeError(INSTALL_HINT)
    with tempfile.TemporaryDirectory(prefix="pdfeditor-office-") as tmp:
        # a private profile, so a running LibreOffice doesn't swallow the request
        profile = Path(tmp, "profile").as_uri()
        cmd = [
            str(soffice),
            f"-env:UserInstallation={profile}",
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            tmp,
            str(source),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"LibreOffice took longer than {timeout:.0f}s") from exc
        out = Path(tmp) / (source.stem + ".pdf")
        if proc.returncode != 0 or not out.exists():
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"LibreOffice could not convert {source.name}. {detail}".strip())
        return out.read_bytes()
