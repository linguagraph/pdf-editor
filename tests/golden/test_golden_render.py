"""Rendering regression tests against reference PNGs in tests/golden/data.

Regenerate after an intended rendering change with:  pytest tests/golden --update-goldens
Failing comparisons write the actual image to tests/golden/_actual/ for inspection.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pdfeditor.engine.base import ColorMode, RenderRequest
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Matrix

DATA = Path(__file__).parent / "data"
ACTUAL = Path(__file__).parent / "_actual"

CASES = [
    ("text_multipage", 0),
    ("images", 0),
    ("vector_art", 0),
    ("annotations", 0),
    ("rotated_pages", 1),
    ("rotated_pages", 4),
    ("cjk_text", 0),
    ("layers", 0),
]

# Tolerances absorb anti-aliasing differences between MuPDF builds/platforms.
MAX_MEAN_DIFF = 1.5
MAX_BAD_PIXEL_FRACTION = 0.004  # pixels differing by more than 64 levels


def render(name: str, page: int, fixture_pdf) -> np.ndarray:
    doc = get_engine().open(fixture_pdf(name))
    try:
        result = doc.page(page).render(RenderRequest(matrix=Matrix.scale(1.0), color=ColorMode.RGB))
    finally:
        doc.close()
    arr = np.frombuffer(result.samples, dtype=np.uint8).reshape(result.height, result.stride)
    return arr[:, : result.width * 3].reshape(result.height, result.width, 3).copy()


@pytest.mark.parametrize(("name", "page"), CASES, ids=[f"{n}-p{p}" for n, p in CASES])
def test_render_matches_golden(name: str, page: int, fixture_pdf, request) -> None:
    actual = render(name, page, fixture_pdf)
    ref_path = DATA / f"{name}-p{page}.png"
    if request.config.getoption("--update-goldens") or not ref_path.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        Image.fromarray(actual).save(ref_path, optimize=True)
        pytest.skip(f"wrote reference {ref_path.name}")
    expected = np.asarray(Image.open(ref_path).convert("RGB"))
    if expected.shape != actual.shape:
        pytest.fail(f"size changed: {expected.shape} -> {actual.shape}")
    diff = np.abs(expected.astype(np.int16) - actual.astype(np.int16))
    mean = float(diff.mean())
    bad = float((diff.max(axis=2) > 64).mean())
    if mean > MAX_MEAN_DIFF or bad > MAX_BAD_PIXEL_FRACTION:
        ACTUAL.mkdir(exist_ok=True)
        Image.fromarray(actual).save(ACTUAL / ref_path.name)
        pytest.fail(f"render differs: mean diff {mean:.2f}, bad pixels {bad:.3%}")
