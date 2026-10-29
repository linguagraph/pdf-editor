"""Microsoft Store listing images: screenshots of the real app, plus the logo and poster.

    uv run python scripts/make_store_images.py [--out dist/store]

Writes to ``dist/store`` (git-ignored):

- ``screenshot-0N-<scene>-<theme>.png``: 1920x1080 shots of the app, taken headless (offscreen
  platform, 125% scale, Segoe UI) with fictional sample documents made by this script. They
  show the start page, editing, comments with the mini toolbar, the page organizer and a
  dialog, in light and dark.
- ``store-logo-300.png``, ``box-art-1x1-2160.png`` and ``box-art-1x1-1080.png``: the 1:1 logo.
- ``poster-2x3-1440x2160.png`` and ``poster-2x3-720x1080.png``: the 2:3 poster.

Settings, recent files and logs go to a temporary folder, so the user's profile is untouched.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRANDING = ROOT / "src" / "pdfeditor" / "data" / "branding" / "elanero"
WORK = Path(tempfile.mkdtemp(prefix="elanero-store-"))

# Must be set before Qt starts: headless, and 125% so a 1536x864 window is 1920x1080 pixels.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = "1.25"
os.environ["PDFEDITOR_DATA_DIR"] = str(WORK / "data")

import pymupdf  # noqa: E402 - dev script, may use PyMuPDF directly
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

WINDOW = (1536, 864)  # logical; 1920x1080 pixels at 125%
SHOT = (1920, 1080)
ACCENT = "#0067c0"
TEAL_DARK = (5, 36, 54)
TEAL_DEEP = (8, 47, 63)
TURQUOISE = (25, 196, 200)
AQUA = (221, 246, 248)
FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


# -- sample documents -------------------------------------------------------------------------
CSS = """
* { font-family: sans-serif; color: #1d2733; }
h1 { font-size: 26px; color: #0b3b5c; margin: 0 0 4px 0; }
h2 { font-size: 15px; color: #0b6e8a; margin: 14px 0 4px 0; }
p { font-size: 10.5px; line-height: 1.45; margin: 0 0 7px 0; text-align: justify; }
.sub { font-size: 12px; color: #5b6b7b; margin-bottom: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 10px; }
th { background: #0b6e8a; color: white; text-align: left; padding: 4px 6px; }
th * { color: white; }
td { border-bottom: 1px solid #d5dde5; padding: 4px 6px; }
"""

PARAGRAPHS = [
    "Harbor Light finished the year with revenue of $4.82 million, up 18 percent on last year. "
    "Growth came from three places: the two new cafés on the waterfront, a wholesale programme "
    "that now supplies 41 restaurants and offices, and online subscriptions, which more than "
    "doubled after the redesigned ordering site launched in April.",
    "Gross margin improved from 61 to 64 percent. Direct trade with three cooperatives in "
    "Colombia and Ethiopia kept green coffee prices stable through a volatile market, and the "
    "new roaster cut energy use per kilogram by a fifth.",
    "We opened our first training kitchen in September. Forty-two baristas completed the "
    "eight-week programme, and twenty-nine of them now work in our cafés. Staff turnover fell "
    "to 22 percent, the lowest since the company was founded.",
    "Next year we plan to open one more café, extend the subscription range to cold brew and "
    "decaffeinated blends, and publish our first full sustainability report, including "
    "packaging, transport and farm-level data from our partners.",
]


def _chart(page: pymupdf.Page, top: float) -> None:
    """A bar chart of quarterly revenue, drawn as vector shapes."""
    left, width, height = 72, 451, 150
    page.draw_rect(
        pymupdf.Rect(left, top, left + width, top + height + 40), color=(0.85, 0.88, 0.9), width=0.6
    )
    values = [("Q1", 0.98, 0.82), ("Q2", 1.12, 0.95), ("Q3", 1.26, 1.04), ("Q4", 1.46, 1.27)]
    peak = 1.6
    base = top + height + 10
    for i, (label, now, before) in enumerate(values):
        x = left + 40 + i * 100
        for j, (value, colour) in enumerate(
            ((before, (0.62, 0.78, 0.84)), (now, (0.04, 0.43, 0.54)))
        ):
            h = value / peak * (height - 20)
            page.draw_rect(
                pymupdf.Rect(x + j * 26, base - h, x + j * 26 + 22, base), color=None, fill=colour
            )
        page.insert_text((x + 14, base + 14), label, fontsize=9, color=(0.3, 0.36, 0.42))
    page.draw_line((left + 30, base), (left + width - 20, base), color=(0.6, 0.65, 0.7), width=0.6)
    page.insert_text(
        (left + 12, top + 18),
        "Revenue by quarter ($ million)",
        fontsize=10,
        color=(0.04, 0.23, 0.36),
    )
    page.draw_rect(
        pymupdf.Rect(left + 300, top + 10, left + 308, top + 18),
        color=None,
        fill=(0.62, 0.78, 0.84),
    )
    page.insert_text((left + 312, top + 18), "2025", fontsize=8.5)
    page.draw_rect(
        pymupdf.Rect(left + 350, top + 10, left + 358, top + 18),
        color=None,
        fill=(0.04, 0.43, 0.54),
    )
    page.insert_text((left + 362, top + 18), "2026", fontsize=8.5)


def _footer(page: pymupdf.Page, number: int) -> None:
    page.insert_text(
        (72, 810),
        "Harbor Light Coffee Co. · Annual Report 2026",
        fontsize=8,
        color=(0.45, 0.5, 0.55),
    )
    page.insert_text((508, 810), str(number), fontsize=8, color=(0.45, 0.5, 0.55))


def make_report(path: Path, comments: bool) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    html = (
        "<h1>Annual Report 2026</h1><div class='sub'>Harbor Light Coffee Co.</div>"
        "<h2>A year of steady growth</h2>" + "".join(f"<p>{p}</p>" for p in PARAGRAPHS[:2])
    )
    page.insert_htmlbox(pymupdf.Rect(72, 72, 523, 330), html, css=CSS)
    _chart(page, 340)
    page.insert_htmlbox(
        pymupdf.Rect(72, 545, 523, 790), "<h2>People</h2>" + f"<p>{PARAGRAPHS[2]}</p>", css=CSS
    )
    _footer(page, 1)

    page = doc.new_page(width=595, height=842)
    rows = [
        ("Waterfront café", "1,240", "1,610", "+30%"),
        ("Old Town café", "980", "1,020", "+4%"),
        ("Station kiosk", "410", "455", "+11%"),
        ("Wholesale", "860", "1,050", "+22%"),
        ("Online subscriptions", "290", "685", "+136%"),
        ("Total", "3,780", "4,820", "+18%"),
    ]
    table = "".join(
        f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{d}</td></tr>" for a, b, c, d in rows
    )
    html = (
        "<h2>Revenue by channel</h2>"
        "<table><tr><th>Channel</th><th>2025 ($k)</th><th>2026 ($k)</th><th>Change</th></tr>"
        f"{table}</table><h2>Looking ahead</h2><p>{PARAGRAPHS[3]}</p>"
        + "".join(f"<p>{p}</p>" for p in PARAGRAPHS[:3])
    )
    page.insert_htmlbox(pymupdf.Rect(72, 72, 523, 790), html, css=CSS)
    _footer(page, 2)

    for n in range(3, 9):
        page = doc.new_page(width=595, height=842)
        order = PARAGRAPHS[n % 4 :] + PARAGRAPHS[: n % 4]
        heading = ["Sourcing", "Our cafés", "Wholesale", "Online", "Sustainability", "Outlook"][
            n - 3
        ]
        page.insert_htmlbox(
            pymupdf.Rect(72, 72, 523, 790),
            f"<h2>{heading}</h2>" + "".join(f"<p>{p}</p>" for p in order + order[:2]),
            css=CSS,
        )
        if n in (4, 7):
            _chart(page, 560)
        _footer(page, n)

    if comments:
        _add_comments(doc)
    doc.set_metadata({"title": "Annual Report 2026", "author": "Harbor Light Coffee Co."})
    doc.save(path, garbage=3, deflate=True)


def _add_comments(doc: pymupdf.Document) -> None:
    page = doc[0]
    for phrase, author, note, colour in (
        (
            "revenue of $4.82 million",
            "Maria Chen",
            "Can we show this against the plan?",
            (1, 0.85, 0.2),
        ),
        (
            "Gross margin improved from 61 to 64 percent",
            "David Okafor",
            "Great result, worth a callout box.",
            (0.55, 0.85, 0.45),
        ),
        ("new roaster", "Maria Chen", "Add the model and install date.", (1, 0.85, 0.2)),
    ):
        quads = page.search_for(phrase, quads=True)
        if quads:
            annot = page.add_highlight_annot(quads)
            annot.set_colors(stroke=colour)
            annot.set_info(title=author, content=note)
            annot.update()
    note = page.add_text_annot(
        (535, 300), "Please check these figures with finance before we publish."
    )
    note.set_info(title="Sofia Lindqvist")
    note.update()
    strike = page.search_for("eight-week", quads=True)
    if strike:
        annot = page.add_strikeout_annot(strike)
        annot.set_info(title="David Okafor", content="It's ten weeks from next year.")
        annot.update()


def make_letter(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(0, 0, 595, 90), color=None, fill=(0.04, 0.43, 0.54))
    page.insert_text((72, 55), "NORTHWIND STUDIO", fontsize=20, color=(1, 1, 1))
    html = "<h1>Lease Agreement</h1><div class='sub'>Unit 4B, 18 Canal Street</div>" + "".join(
        f"<h2>Clause {i}</h2><p>{PARAGRAPHS[i % 4]}</p>" for i in range(1, 5)
    )
    page.insert_htmlbox(pymupdf.Rect(72, 120, 523, 790), html, css=CSS)
    doc.save(path)


def make_brochure(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.draw_rect(page.rect, color=None, fill=(0.96, 0.93, 0.87))
    page.draw_rect(pymupdf.Rect(0, 0, 330, 595), color=None, fill=(0.85, 0.42, 0.25))
    page.insert_text((40, 120), "Autumn", fontsize=46, color=(1, 1, 1))
    page.insert_text((40, 170), "Workshops", fontsize=46, color=(1, 1, 1))
    page.insert_text((40, 520), "Ceramics · Weaving · Print", fontsize=14, color=(1, 1, 1))
    for i in range(3):
        page.draw_circle(
            (480 + i * 120, 200), 48, color=None, fill=(0.95 - i * 0.2, 0.7, 0.4 + i * 0.15)
        )
    page.insert_htmlbox(
        pymupdf.Rect(370, 300, 800, 560), "".join(f"<p>{p}</p>" for p in PARAGRAPHS[:2]), css=CSS
    )
    doc.save(path)


def make_slides(path: Path) -> None:
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page(width=842, height=473)
        page.draw_rect(page.rect, color=None, fill=(0.07, 0.15, 0.24))
        page.insert_text(
            (60, 120),
            ["Product Roadmap", "Customer Research", "Next Steps"][n],
            fontsize=34,
            color=(1, 1, 1),
        )
        page.draw_rect(pymupdf.Rect(60, 150, 260, 156), color=None, fill=(0.1, 0.77, 0.78))
    doc.save(path)


# -- screenshots ------------------------------------------------------------------------------
def take_screenshots(out: Path) -> list[Path]:
    from PySide6.QtCore import QCoreApplication, QPoint, QRect, QSettings, Qt
    from PySide6.QtGui import QColor, QCursor, QFont, QFontDatabase, QImage, QPainter, QPainterPath
    from PySide6.QtWidgets import QApplication, QWidget

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(WORK / "settings"))
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Elanero")
    app.setOrganizationName("Elanero")

    from pdfeditor.core.engine_lock import install_manual_gc
    from pdfeditor.services.text import TextPos, TextSelection
    from pdfeditor.ui.dialogs.pages import HeaderFooterDialog
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.settings import AppSettings
    from pdfeditor.ui.theme import Theme, apply_theme

    install_manual_gc()
    for name in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf"):
        if (FONTS / name).is_file():
            QFontDatabase.addApplicationFont(str(FONTS / name))  # offscreen sees no system fonts
    app.setFont(QFont("Segoe UI", 9))
    QApplication.setCursorFlashTime(0)
    QCursor.setPos(QPoint(-5000, -5000))

    docs = WORK / "Documents"
    docs.mkdir(parents=True)
    report = docs / "Annual Report 2026.pdf"
    make_report(report, comments=True)
    make_letter(docs / "Lease Agreement.pdf")
    make_brochure(docs / "Autumn Workshops.pdf")
    make_slides(docs / "Product Roadmap.pdf")
    plain = WORK / "Annual Report 2026.pdf"
    make_report(plain, comments=False)
    recent = [
        docs / n
        for n in (
            "Annual Report 2026.pdf",
            "Lease Agreement.pdf",
            "Autumn Workshops.pdf",
            "Product Roadmap.pdf",
        )
    ]

    def wait(ms: int = 60, window: MainWindow | None = None) -> None:
        for _ in range(40):
            busy = False
            if window is not None:
                window.renderer.wait_idle(200)
                busy = bool(window.renderer._pending) or window.start_page.thumbnails_pending()
            for _ in range(5):
                app.processEvents()
            if not busy:
                break
        end = ms
        while end > 0:
            app.processEvents()
            QThreadSleep(10)
            end -= 10

    def QThreadSleep(ms: int) -> None:
        from PySide6.QtCore import QThread

        QThread.msleep(ms)

    def make_window(theme: str) -> MainWindow:
        settings = QSettings()
        settings.setValue("recent_files", [str(p) for p in recent])
        settings.setValue("recent_pinned", [str(recent[0])])
        prefs = AppSettings()
        prefs.theme = theme
        prefs.accent = ACCENT
        window = MainWindow()
        window.resize(*WINDOW)
        window.show()
        apply_theme(app, Theme(theme), ACCENT)
        window.pill.idle_ms = 24 * 3600 * 1000
        window.show_panel(window.panels[0])  # Pages, whatever the last scene left open
        wait(200, window)
        return window

    def grab(widget: QWidget) -> QImage:
        image = widget.grab().toImage()
        return (
            image.scaled(
                *SHOT,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if image.size().width() != SHOT[0]
            else image
        )

    def close(window: MainWindow) -> None:
        for view in window.views():
            view.session.undo_stack.set_clean()
        window.close()
        window.deleteLater()
        wait(50)

    def open_doc(
        window: MainWindow, path: Path, page: int = 0, zoom: float = 1.1, panel: bool = True
    ) -> None:
        view = window.open_path(path)
        assert view is not None, path
        wait(300, window)
        view.set_zoom(zoom)
        wait(100, window)
        view.go_to_page(page, record=False)
        if panel:
            window.show_panel(window.panels[0])  # Pages, whatever the last run left open
        window.pill.poke()
        wait(300, window)

    shots: list[Path] = []

    def save(image: QImage, name: str) -> None:
        path = out / f"screenshot-{len(shots) + 1:02d}-{name}.png"
        image.convertToFormat(QImage.Format.Format_RGB888).save(str(path))
        assert image.width() == SHOT[0] and image.height() == SHOT[1], (path, image.size())
        shots.append(path)
        print(f"  {path.name}")

    def scene_start(theme: str) -> None:
        w = make_window(theme)
        wait(600, w)
        save(grab(w), f"start-{theme}")
        close(w)

    def scene_editing(theme: str) -> None:
        w = make_window(theme)
        open_doc(w, plain)
        w.tool_actions["edit"].trigger()
        wait(400, w)
        save(grab(w), f"editing-{theme}")
        close(w)

    def scene_comments(theme: str) -> None:
        w = make_window(theme)
        w.ribbon.bar.setCurrentIndex(w.ribbon.tab_names().index("Comment"))
        open_doc(w, report, panel=False)
        w.show_panel(w.comments_panel)
        view = w.current_view()
        assert view is not None
        w.activateWindow()
        view.setFocus()
        wait(200, w)
        # select the second paragraph's first sentence so the mini toolbar appears
        text = view.text_cache.get(0).text
        start = max(0, text.find("Direct trade"))
        view.set_selection(TextSelection(TextPos(0, start), TextPos(0, start + 64)))
        wait(400, w)
        save(grab(w), f"comments-{theme}")
        close(w)

    def scene_organize(theme: str) -> None:
        w = make_window(theme)
        w.ribbon.bar.setCurrentIndex(w.ribbon.tab_names().index("Organize"))
        open_doc(w, plain)
        w.organize.act_organize.trigger()
        wait(800, w)
        save(grab(w), f"organize-{theme}")
        close(w)

    def scene_dialog(theme: str) -> None:
        w = make_window(theme)
        w.ribbon.bar.setCurrentIndex(w.ribbon.tab_names().index("Organize"))
        open_doc(w, plain, 1)
        dialog = HeaderFooterDialog(8, 1, [1], parent=w)
        texts = {
            "header_left": "Harbor Light",
            "header_right": "Annual Report",
            "footer_center": "<<page>> / <<pages>>",
            "footer_right": "<<date>>",
        }
        for slot, edit in dialog.slots.items():
            edit.setText(texts.get(slot.value, ""))
            edit.setCursorPosition(0)
        dialog.show()
        wait(300, w)
        base = grab(w)
        top = dialog.grab().toImage()
        # paint in pixels: both grabs carry the 125% device pixel ratio
        base.setDevicePixelRatio(1)
        top.setDevicePixelRatio(1)
        # draw the dialog centred over the window, with a soft shadow, as Windows shows it
        painter = QPainter(base)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = (base.width() - top.width()) // 2
        y = (base.height() - top.height()) // 2 + 20
        for i in range(14, 0, -2):
            path = QPainterPath()
            path.addRoundedRect(
                QRect(x - i, y - i + 6, top.width() + 2 * i, top.height() + 2 * i), 10, 10
            )
            painter.fillPath(path, QColor(0, 0, 0, 10))
        painter.drawImage(x, y, top)
        painter.end()
        dialog.close()
        save(base, f"dialog-{theme}")
        close(w)

    scenes: list[tuple[Callable[[str], None], str]] = [
        (scene_start, "light"),
        (scene_editing, "light"),
        (scene_comments, "dark"),
        (scene_organize, "light"),
        (scene_dialog, "dark"),
        (scene_comments, "light"),
        (scene_start, "dark"),
    ]
    for scene, theme in scenes:
        scene(theme)
    return shots


# -- logo and poster --------------------------------------------------------------------------
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
        if (FONTS / name).is_file():
            return ImageFont.truetype(str(FONTS / name), size)
    return ImageFont.load_default(size)


def _gradient(
    size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]
) -> Image.Image:
    _, h = size
    column = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        column.putpixel(
            (0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
        )
    return column.resize(size)


def _glow(canvas: Image.Image, centre: tuple[int, int], radius: int) -> None:
    glow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(glow).ellipse(
        (centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius), fill=90
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 2))
    canvas.paste(Image.new("RGB", canvas.size, TURQUOISE), (0, 0), glow)


def _icon(size: int) -> Image.Image:
    return (
        Image.open(BRANDING / "source" / "elanero-master-1024.png")
        .convert("RGBA")
        .resize((size, size), Image.Resampling.LANCZOS)
    )


def make_logos(out: Path) -> list[Path]:
    written = []
    # Store logo: the icon alone on a transparent background, as the Store tile shows it.
    logo = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    logo.alpha_composite(_icon(270), (15, 15))
    written.append(out / "store-logo-300.png")
    logo.save(written[-1])

    box = _gradient((2160, 2160), TEAL_DEEP, TEAL_DARK)
    _glow(box, (1080, 1000), 760)
    box = box.convert("RGBA")
    box.alpha_composite(_icon(1300), (430, 330))
    draw = ImageDraw.Draw(box)
    title = _font(190, bold=True)
    draw.text((1080, 1830), "Elanero", font=title, fill="white", anchor="mm")
    written.append(out / "box-art-1x1-2160.png")
    box.convert("RGB").save(written[-1])
    written.append(out / "box-art-1x1-1080.png")
    box.convert("RGB").resize((1080, 1080), Image.Resampling.LANCZOS).save(written[-1])

    poster = _gradient((1440, 2160), TEAL_DEEP, TEAL_DARK)
    _glow(poster, (720, 760), 620)
    poster = poster.convert("RGBA")
    poster.alpha_composite(_icon(900), (270, 330))
    draw = ImageDraw.Draw(poster)
    draw.text((720, 1440), "Elanero", font=_font(200, bold=True), fill="white", anchor="mm")
    draw.text((720, 1600), "PDF EDITOR", font=_font(78), fill=AQUA, anchor="mm")
    draw.text(
        (720, 1900),
        "Edit · Comment · Organize · Convert",
        font=_font(56),
        fill=(150, 220, 226),
        anchor="mm",
    )
    written.append(out / "poster-2x3-1440x2160.png")
    poster.convert("RGB").save(written[-1])
    written.append(out / "poster-2x3-720x1080.png")
    poster.convert("RGB").resize((720, 1080), Image.Resampling.LANCZOS).save(written[-1])
    for path in written:
        print(f"  {path.name}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "store")
    parser.add_argument("--images-only", action="store_true", help="skip the screenshots")
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    try:
        print("Logo and poster:")
        make_logos(out)
        if not args.images_only:
            print("Screenshots:")
            take_screenshots(out)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
