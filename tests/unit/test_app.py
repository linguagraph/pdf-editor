from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from pdfeditor import app


def test_setup_logging_writes_file(tmp_path: Path) -> None:
    log_file = app.setup_logging(log_dir=tmp_path)
    logging.getLogger("pdfeditor.test").info("hello log")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello log" in log_file.read_text(encoding="utf-8")


def test_crash_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "data_dir", lambda: tmp_path)
    try:
        raise ValueError("boom")
    except ValueError:
        exc_type, exc, tb = sys.exc_info()
        assert exc_type is not None and exc is not None
        report = app.write_crash_report(exc_type, exc, tb)
    text = report.read_text(encoding="utf-8")
    assert "ValueError: boom" in text
    assert report.parent == tmp_path / "crashes"
