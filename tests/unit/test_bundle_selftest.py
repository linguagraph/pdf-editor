from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pdfeditor.bundle import data_path, is_frozen


def test_bundled_data_is_found() -> None:
    assert data_path("selftest.pdf").read_bytes().startswith(b"%PDF-")
    assert data_path("icon.png").stat().st_size > 0
    assert not is_frozen()
    with pytest.raises(FileNotFoundError):
        data_path("does-not-exist.bin")


def test_self_test_passes_from_source(tmp_path) -> None:
    report = tmp_path / "report.txt"
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, "-m", "pdfeditor", "--self-test", "--self-test-report", str(report)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = report.read_text(encoding="utf-8")
    assert proc.returncode == 0, text
    assert "RESULT: OK" in text and text.count("[PASS]") == 10
