"""The Elanero branding pack: kept byte for byte, and wired into the icon, splash and About."""

from __future__ import annotations

import hashlib
import json

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication

from pdfeditor import APP_DISPLAY_NAME
from pdfeditor.bundle import data_path
from pdfeditor.ui import branding

PACK = data_path("branding", "elanero")


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pack_is_unchanged() -> None:
    """Every file matches the pack's SHA256SUMS, and the 21 originals its manifest."""
    sums = {}
    for row in (PACK / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = row.strip().split("  ", 1)
        sums[name] = digest
    assert len(sums) == 25
    for name, digest in sums.items():
        assert sha256(PACK / name) == digest, name
    manifest = json.loads((PACK / "ASSET_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["original_file_count"] == len(manifest["files"]) == 21
    for item in manifest["files"]:
        assert sums[item["path"]] == item["sha256"], item["path"]
        assert (PACK / item["path"]).stat().st_size == item["bytes"], item["path"]


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (QSize(1920, 1040), QSize(960, 600)),  # fits: the design size
        (QSize(1024, 728), QSize(819, 512)),  # small screen: 80% of its height, still 16:10
        (QSize(800, 1200), QSize(640, 400)),  # narrow: limited by the width
    ],
)
def test_splash_size_fits_the_screen(available: QSize, expected: QSize) -> None:
    size = branding.splash_logical_size(available)
    assert size == expected
    assert abs(size.width() / size.height() - 1.6) < 0.01


@pytest.mark.gui
def test_splash_follows_the_theme(qtbot, monkeypatch) -> None:
    assert branding.splash_scheme("light") == "light"
    assert branding.splash_scheme("dark") == "dark"
    hints = QGuiApplication.styleHints()
    monkeypatch.setattr(hints, "colorScheme", lambda: Qt.ColorScheme.Dark)
    assert branding.splash_scheme("system") == "dark"
    monkeypatch.setattr(hints, "colorScheme", lambda: Qt.ColorScheme.Light)
    assert branding.splash_scheme("system") == "light"
    for scheme in ("light", "dark"):
        assert branding.splash_image(scheme).name == f"elanero-splash-{scheme}-1920x1200.png"


@pytest.mark.gui
def test_splash_pixmap_is_dpi_aware(qtbot) -> None:
    screen = QGuiApplication.primaryScreen()
    pixmap = branding.splash_pixmap("dark", screen)
    ratio = screen.devicePixelRatio()
    logical = branding.splash_logical_size(screen.availableGeometry().size())
    assert pixmap.devicePixelRatio() == ratio
    assert abs(pixmap.width() - logical.width() * ratio) <= 1
    assert abs(pixmap.height() - logical.height() * ratio) <= 1


@pytest.mark.gui
def test_show_splash_and_finish(qtbot) -> None:
    from PySide6.QtWidgets import QWidget

    splash = branding.show_splash("light")
    assert splash.isVisible() and not splash.pixmap().isNull()
    window = QWidget()
    qtbot.addWidget(window)
    window.show()
    splash.finish(window)
    qtbot.waitUntil(lambda: not splash.isVisible())


@pytest.mark.gui
def test_app_icon_has_every_size(qtbot) -> None:
    icon = branding.app_icon()
    sizes = sorted(s.width() for s in icon.availableSizes())
    assert sizes == list(branding.ICON_SIZES)


@pytest.mark.gui
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_about_shows_the_brand(qtbot, theme: str) -> None:
    from PySide6.QtWidgets import QApplication

    from pdfeditor.ui.dialogs.about import AboutDialog
    from pdfeditor.ui.theme import Theme, apply_theme

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    apply_theme(app, Theme(theme))
    try:
        d = AboutDialog("PyMuPDF test")
        qtbot.addWidget(d)
        assert d.header_title.text() == APP_DISPLAY_NAME
        assert d.windowTitle() == "About Elanero"
        pixmap = d.brand.pixmap()
        assert not pixmap.isNull()
        width = pixmap.width() / pixmap.devicePixelRatio()
        height = pixmap.height() / pixmap.devicePixelRatio()
        if theme == "light":  # the wordmark (10:3)
            assert round(height) == 72 and abs(width / height - 10 / 3) < 0.05
        else:  # its teal lettering isn't legible on dark: the icon alone
            assert round(width) == round(height) == 64
        assert APP_DISPLAY_NAME in d.tabs.widget(0).toPlainText()
    finally:
        apply_theme(app, Theme.SYSTEM)
