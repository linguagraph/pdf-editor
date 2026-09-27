from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from pdfeditor.model.geometry import Point
from pdfeditor.services.custom_stamps import (
    StampImageError,
    StampLibrary,
    image_size,
    stamp_rect,
)


def _image(fmt: str, size: tuple[int, int] = (64, 32), **save: object) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 0, 0)).save(out, fmt, **save)
    return out.getvalue()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_image("PNG"), (64, 32)),
        (_image("JPEG", (33, 17)), (33, 17)),
        (_image("JPEG", (40, 50), progressive=True), (40, 50)),
        (b"GIF89a....", None),
        (b"\xff\xd8\xff", None),  # truncated JPEG
        (b"", None),
    ],
)
def test_image_size(data: bytes, expected: tuple[int, int] | None) -> None:
    assert image_size(data) == expected


def test_stamp_rect_is_centered_and_capped() -> None:
    small = stamp_rect(Point(100, 100), (80, 40))
    assert (small.width, small.height) == (60, 30)
    assert small.center == Point(100, 100)
    big = stamp_rect(Point(0, 0), (2000, 1000))
    assert big.width == pytest.approx(200) and big.height == pytest.approx(100)


def test_library_add_order_get_remove(tmp_path: Path) -> None:
    lib = StampLibrary(tmp_path / "stamps")
    assert lib.stamps() == []
    src = tmp_path / "Signed by me.png"
    src.write_bytes(_image("PNG"))
    first = lib.add(src)
    second = lib.add(src)  # same name again: kept apart
    jpeg = tmp_path / "photo.jpeg"
    jpeg.write_bytes(_image("JPEG"))
    third = lib.add(jpeg, label="Seal/../x")
    assert first.key == "Signed by me.png" and first.label == "Signed by me"
    assert second.key == "Signed by me (2).png"
    assert third.key.endswith(".jpg") and "/" not in third.key and third.path.parent == lib.folder
    assert first.read() == src.read_bytes()
    # remembered order first, then the rest by name
    keys = [s.key for s in lib.stamps([third.key, "missing.png", first.key])]
    assert keys == [third.key, first.key, second.key]
    assert lib.get("../../etc/passwd") is None
    assert lib.get(second.key) == second
    lib.remove(second.key)
    assert lib.get(second.key) is None
    assert [s.key for s in lib.stamps()] == sorted([first.key, third.key], key=str.lower)


def test_library_rejects_non_images(tmp_path: Path) -> None:
    lib = StampLibrary(tmp_path / "stamps")
    bogus = tmp_path / "x.png"
    bogus.write_bytes(b"hello")
    with pytest.raises(StampImageError):
        lib.add(bogus)
    with pytest.raises(StampImageError):
        lib.add(tmp_path / "does-not-exist.png")
    assert lib.stamps() == []


def test_default_folder_is_in_data_dir() -> None:
    from pdfeditor.core.paths import data_dir

    assert StampLibrary().folder == data_dir() / "stamps"
