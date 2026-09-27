"""Pages to images (PNG / JPEG / TIFF) and extraction of embedded images and fonts."""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import ColorMode, Document, RenderRequest
from pdfeditor.model.geometry import Matrix


class ImageFormat(Enum):
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"

    @property
    def suffix(self) -> str:
        return {"png": ".png", "jpeg": ".jpg", "tiff": ".tif"}[self.value]


@dataclass(frozen=True)
class PageImageOptions:
    format: ImageFormat = ImageFormat.PNG
    dpi: int = 150
    grayscale: bool = False
    jpeg_quality: int = 90
    annotations: bool = True
    multipage_tiff: bool = True  # TIFF: all pages in one file


def render_page(doc: Document, index: int, options: PageImageOptions) -> Image.Image:
    color = ColorMode.GRAY if options.grayscale else ColorMode.RGB
    shot = doc.page(index).render(
        RenderRequest(
            matrix=Matrix.scale(options.dpi / 72), color=color, annotations=options.annotations
        )
    )
    mode = "L" if options.grayscale else "RGB"
    img = Image.frombytes(mode, (shot.width, shot.height), shot.samples, "raw", mode, shot.stride)
    img.info["dpi"] = (options.dpi, options.dpi)
    return img


def _save(img: Image.Image, target: Path, options: PageImageOptions) -> None:
    kwargs: dict[str, object] = {"dpi": (options.dpi, options.dpi)}
    if options.format is ImageFormat.JPEG:
        kwargs.update(quality=options.jpeg_quality, optimize=True)
    elif options.format is ImageFormat.TIFF:
        kwargs["compression"] = "tiff_deflate"
    img.save(target, format=options.format.value.upper(), **kwargs)


def export_pages(
    doc: Document,
    pages: Sequence[int],
    target: Path,
    options: PageImageOptions,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> list[Path]:
    """Write ``target`` itself for a single image (one page, or a multi-page TIFF); otherwise
    ``<stem>-<page number>.<ext>`` next to it."""
    target = target.with_suffix(options.format.suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    single_file = options.format is ImageFormat.TIFF and options.multipage_tiff
    width = len(str(max(pages) + 1)) if pages else 1
    written: list[Path] = []
    frames: list[Image.Image] = []
    for n, index in enumerate(pages):
        if token is not None:
            token.check()
        img = render_page(doc, index, options)
        if single_file:
            frames.append(img)
        else:
            path = (
                target
                if len(pages) == 1
                else target.with_name(f"{target.stem}-{index + 1:0{width}d}{target.suffix}")
            )
            _save(img, path, options)
            written.append(path)
        progress(n + 1, len(pages))
    if single_file and frames:
        frames[0].save(
            target,
            format="TIFF",
            save_all=True,
            append_images=frames[1:],
            compression="tiff_deflate",
            dpi=(options.dpi, options.dpi),
        )
        written.append(target)
    return written


def _unique(folder: Path, name: str) -> Path:
    path = folder / name
    k = 2
    while path.exists():
        path = folder / f"{Path(name).stem}-{k}{Path(name).suffix}"
        k += 1
    return path


def extract_images(
    doc: Document,
    folder: Path,
    pages: Sequence[int] | None = None,
    min_size: int = 16,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> list[Path]:
    """Save each distinct image (shown on ``pages``, default all) in its own format."""
    folder.mkdir(parents=True, exist_ok=True)
    wanted = set(pages) if pages is not None else None
    infos = [
        i
        for i in doc.images()
        if (wanted is None or wanted.intersection(i.pages))
        and i.width >= min_size
        and i.height >= min_size
    ]
    written: list[Path] = []
    for n, info in enumerate(infos):
        if token is not None:
            token.check()
        data, ext = doc.extract_image(info.ref)
        if ext == "jpx":  # JPEG 2000 is poorly supported by viewers
            buf = io.BytesIO()
            Image.open(io.BytesIO(data)).save(buf, format="PNG")
            data, ext = buf.getvalue(), "png"
        name = f"page{info.pages[0] + 1}-image{n + 1}.{'jpg' if ext == 'jpeg' else ext}"
        path = _unique(folder, name)
        path.write_bytes(data)
        written.append(path)
        progress(n + 1, len(infos))
    return written


def extract_fonts(
    doc: Document,
    folder: Path,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> tuple[list[Path], list[str]]:
    """Save every embedded font program; returns (written files, names of fonts not embedded)."""
    folder.mkdir(parents=True, exist_ok=True)
    fonts = doc.fonts()
    written: list[Path] = []
    skipped: list[str] = []
    for n, font in enumerate(fonts):
        if token is not None:
            token.check()
        if not font.embedded:
            skipped.append(font.name)
            continue
        try:
            name, data = doc.extract_font(font.ref)
        except ValueError:
            skipped.append(font.name)
            continue
        path = _unique(folder, name)
        path.write_bytes(data)
        written.append(path)
        progress(n + 1, len(fonts))
    return written, skipped
