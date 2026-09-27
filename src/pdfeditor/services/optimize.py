"""Reduce file size: presets, a space audit, and optimizing a copy of the document."""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace

import pikepdf

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import Document, OptimizeOptions, SaveOptions
from pdfeditor.model.metadata import SpaceUsage


@dataclass(frozen=True)
class ReduceOptions:
    optimize: OptimizeOptions = field(default_factory=OptimizeOptions)
    object_streams: bool = True
    clean_content: bool = True  # rewrite content streams compactly
    linearize: bool = False  # fast web view


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    options: ReduceOptions


PRESETS = (
    Preset(
        "screen",
        "Smallest (screen)",
        "Images at 72 dpi, JPEG quality 50: for on-screen reading and email.",
        ReduceOptions(OptimizeOptions(image_dpi=72, jpeg_quality=50)),
    ),
    Preset(
        "ebook",
        "Balanced (ebook)",
        "Images at 150 dpi, JPEG quality 70: good on screen, fine for office printing.",
        ReduceOptions(OptimizeOptions(image_dpi=150, jpeg_quality=70)),
    ),
    Preset(
        "print",
        "High quality (print)",
        "Images at 300 dpi, JPEG quality 85: keeps print quality, still drops the waste.",
        ReduceOptions(OptimizeOptions(image_dpi=300, jpeg_quality=85)),
    ),
    Preset(
        "lossless",
        "Lossless",
        "Images untouched; subsets fonts, removes unused and duplicate objects, compresses.",
        ReduceOptions(OptimizeOptions(image_dpi=None, jpeg_quality=100)),
    ),
)


def preset(key: str) -> Preset:
    return next(p for p in PRESETS if p.key == key)


@dataclass
class ReduceResult:
    data: bytes
    before: int
    after: int
    notes: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.before - self.after

    @property
    def ratio(self) -> float:
        return self.after / self.before if self.before else 1.0


def audit(doc: Document) -> SpaceUsage:
    return doc.space_usage()


def linearize(data: bytes) -> bytes:
    """Rewrite for fast web view (qpdf). Encrypted files are not supported (qpdf would drop
    the encryption), so callers must check first."""
    with pikepdf.open(io.BytesIO(data)) as pdf:
        out = io.BytesIO()
        pdf.save(
            out,
            linearize=True,
            object_stream_mode=pikepdf.ObjectStreamMode.preserve,
            compress_streams=True,
        )
    return out.getvalue()


def _is_encrypted(data: bytes) -> bool:
    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            return bool(pdf.is_encrypted)
    except pikepdf.PasswordError:
        return True


def reduce_size(
    doc: Document,
    options: ReduceOptions,
    before: int | None = None,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> ReduceResult:
    """Optimize a copy of ``doc`` (the open document is not changed) and return the new file.

    ``before`` is the size to compare with (the file on disk); by default what a normal save
    would write now.
    """
    steps = 4 if options.linearize else 3

    def step(n: int) -> None:
        if token is not None:
            token.check()
        progress(n, steps)

    if before is None:
        before = len(doc.to_bytes(SaveOptions()))
    step(0)
    work = doc.copy()
    try:
        work.optimize(options.optimize)
        step(1)
        data = work.to_bytes(
            SaveOptions(
                garbage=4,
                deflate=True,
                object_streams=options.object_streams,
                clean_content=options.clean_content,
            )
        )
    finally:
        work.close()
    step(2)
    notes: list[str] = []
    if options.linearize:
        if _is_encrypted(data):
            notes.append("Fast web view was skipped: it isn't supported for encrypted files.")
        else:
            data = linearize(data)
        step(3)
    progress(steps, steps)
    if len(data) >= before:
        notes.append("The file is already compact; these settings don't make it smaller.")
    return ReduceResult(data, before, len(data), notes)


def with_images(options: ReduceOptions, dpi: int | None, quality: int, gray: bool) -> ReduceOptions:
    return replace(
        options,
        optimize=replace(options.optimize, image_dpi=dpi, jpeg_quality=quality, grayscale=gray),
    )
