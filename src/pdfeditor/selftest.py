"""``pdfeditor --self-test``: exercise the main subsystems end to end and report.

Used by CI on the frozen executable (a feature that works from source but not when frozen fails
the build) and by users sending in bug reports. Runs without showing any window.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from pdfeditor import __version__
from pdfeditor.bundle import data_path, is_frozen

Check = Callable[[], str]


def _checks(workdir: Path) -> list[tuple[str, Check]]:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.engine.base import RenderRequest
    from pdfeditor.engine.registry import get_engine
    from pdfeditor.model.geometry import Matrix
    from pdfeditor.services.search import SearchQuery, search_document
    from pdfeditor.services.text import TextIndexCache

    state: dict[str, object] = {}

    def engine() -> str:
        e = get_engine()
        return f"{e.name}: {getattr(e, 'version', '?')}"

    def open_sample() -> str:
        session = DocumentSession.open(data_path("selftest.pdf"))
        state["session"] = session
        return f"{session.page_count} page(s)"

    def render() -> str:
        session = state["session"]
        assert isinstance(session, DocumentSession)
        with session.lock:
            result = session.document.page(0).render(RenderRequest(matrix=Matrix.scale(2)))
        distinct = len(set(result.samples[::101]))
        if distinct < 3:
            raise AssertionError("rendered page is blank")
        return f"{result.width}x{result.height}, {distinct} sampled colors"

    def text_and_search() -> str:
        session = state["session"]
        assert isinstance(session, DocumentSession)
        hits = search_document(TextIndexCache(session), SearchQuery("lazy dog"))
        if len(hits) != 3:
            raise AssertionError(f"expected 3 hits, got {len(hits)}")
        return "3 hits"

    def undo_redo() -> str:
        from pdfeditor.core.commands import SetMetadataCommand

        session = state["session"]
        assert isinstance(session, DocumentSession)
        meta = session.document.metadata()
        meta.subject = "self-test edit"
        session.execute(SetMetadataCommand(meta))
        if not session.is_dirty or not session.undo():
            raise AssertionError("undo failed")
        if session.document.metadata().subject == "self-test edit" or session.is_dirty:
            raise AssertionError("undo didn't restore the document")
        session.redo()
        return "edit, undo, redo"

    def annotations() -> str:
        from pdfeditor.core.commands import AddAnnotationCommand
        from pdfeditor.model.annotations import AnnotationModel, AnnotationType
        from pdfeditor.model.color import Color
        from pdfeditor.model.geometry import Rect

        session = state["session"]
        assert isinstance(session, DocumentSession)
        model = AnnotationModel(
            AnnotationType.SQUARE, 0, Rect(72, 450, 200, 520), color=Color(1, 0, 0)
        )
        session.execute(AddAnnotationCommand(model))
        found = [
            a for a in session.document.page(0).annotations() if a.type is AnnotationType.SQUARE
        ]
        if len(found) != 1:
            raise AssertionError("annotation was not created")
        return "square comment created"

    def page_operations() -> str:
        from pdfeditor.services.stamping import (
            HeaderFooter,
            Slot,
            apply_header_footer,
            trimmed_rect,
        )

        session = state["session"]
        assert isinstance(session, DocumentSession)
        doc = session.document
        doc.select_pages([0, 0])
        spec = HeaderFooter(texts={Slot.FOOTER_CENTER: "<<page>>/<<pages>>"})
        apply_header_footer(session.engine, doc, spec, [0, 1])
        if "2/2" not in doc.page(1).text_page(with_chars=False).text:
            raise AssertionError("footer text missing")
        if trimmed_rect(doc, 0) is None:  # needs Pillow in the bundle
            raise AssertionError("content bounds not found")
        doc.select_pages([0])
        return "duplicate, footer, margin detection"

    def content_editing() -> str:
        from pdfeditor.model.objects import ObjectType

        session = state["session"]
        assert isinstance(session, DocumentSession)
        page = session.document.page(0)
        objects = page.content_objects()
        text = next(
            o for o in objects if o.type is ObjectType.TEXT and o.text.startswith("pdfeditor")
        )
        page.replace_text(text.key, "pdfeditor self-test (edited)")
        if "(edited)" not in session.document.page(0).text_page(with_chars=False).text:
            raise AssertionError("text replacement failed")
        paths = [o for o in session.document.page(0).content_objects() if o.type is ObjectType.PATH]
        return f"{len(objects)} objects, text replaced, {len(paths)} path(s) intact"

    def save_copy() -> str:
        session = state["session"]
        assert isinstance(session, DocumentSession)
        out = workdir / "copy.pdf"
        with session.lock:
            session.document.save(out)
        import pikepdf

        with pikepdf.open(out) as pdf:
            pages = len(pdf.pages)
        session.close()
        return f"saved and verified with qpdf {pikepdf.__libqpdf_version__} ({pages} page)"

    def qt_gui() -> str:
        from PySide6.QtCore import QCoreApplication, QSettings, qVersion
        from PySide6.QtWidgets import QApplication

        from pdfeditor.ui.main_window import MainWindow

        app = QApplication.instance() or QApplication(sys.argv[:1])
        # Keep the self-test's recent files and window state out of the user's settings.
        QCoreApplication.setOrganizationName("pdfeditor-selftest")
        QCoreApplication.setApplicationName("pdfeditor-selftest")
        window = MainWindow()
        view = window.open_path(data_path("selftest.pdf"))
        if view is None:
            raise AssertionError("main window could not open the sample")
        app.processEvents()
        window.renderer.wait_idle(5000)
        window.close()
        QSettings().clear()
        return f"Qt {qVersion()}, main window OK"

    def printing() -> str:
        from pdfeditor.core.session import DocumentSession as Session
        from pdfeditor.ui.printing import PrintOptions, pdf_printer, print_pages

        session = Session.open(data_path("selftest.pdf"))
        out = workdir / "printed.pdf"
        count = print_pages(session, pdf_printer(str(out)), PrintOptions(pages=(0,)))
        session.close()
        if count != 1 or not out.exists():
            raise AssertionError("print to PDF produced no output")
        return f"printed {count} page to PDF"

    return [
        ("engine", engine),
        ("open bundled sample", open_sample),
        ("render", render),
        ("text + search", text_and_search),
        ("undo + redo", undo_redo),
        ("annotations", annotations),
        ("page operations", page_operations),
        ("content editing", content_editing),
        ("save + verify", save_copy),
        ("Qt main window", qt_gui),
        ("print to PDF", printing),
    ]


def run_self_test(report_path: Path | None = None) -> int:
    """Run all checks; returns a process exit code (0 = all passed)."""
    # No window may appear, and a real printer must not be needed.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pdfeditor.core.engine_lock import install_manual_gc

    install_manual_gc()
    lines = [
        f"pdfeditor {__version__} self-test",
        f"frozen: {is_frozen()}  python: {platform.python_version()}  os: {platform.platform()}",
    ]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="pdfeditor-selftest-") as tmp:
        for name, check in _checks(Path(tmp)):
            start = time.perf_counter()
            try:
                detail = check()
                status = "PASS"
            except Exception as exc:
                failures += 1
                detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                status = "FAIL"
            lines.append(f"[{status}] {name} ({time.perf_counter() - start:.2f}s): {detail}")
    lines.append("RESULT: " + ("OK" if failures == 0 else f"{failures} check(s) failed"))
    report = "\n".join(lines) + "\n"
    if report_path is not None:
        report_path.write_text(report, encoding="utf-8")
    if sys.stdout is not None:  # windowed executables have no console
        sys.stdout.write(report)
        sys.stdout.flush()
    return 0 if failures == 0 else 1
