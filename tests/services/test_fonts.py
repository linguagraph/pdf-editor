from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from pdfeditor.core.jobs import Cancelled, CancelToken
from pdfeditor.model.fonts import FontRef
from pdfeditor.services import fonts as fonts_service
from pdfeditor.services.fonts import (
    FontCatalog,
    installed_ref_for_font_name,
    normalize_font_name,
    read_faces,
    system_font_dirs,
    text_scripts,
)


@pytest.fixture
def fonts_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "fonts"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDFEDITOR_DATA_DIR", str(tmp_path / "data"))


# -- reading faces --------------------------------------------------------------------------------


def test_read_faces_family_style_weight_italic(fonts_dir: Path) -> None:
    regular = read_faces(fonts_dir / "TestSans-Regular.ttf")
    assert len(regular) == 1
    face = regular[0]
    assert face.family == "Test Sans"
    assert face.style == "Regular"
    assert face.weight == 400
    assert face.italic is False
    assert face.embeddable is True
    assert face.variable is False

    bold = read_faces(fonts_dir / "TestSans-Bold.ttf")[0]
    assert bold.weight == 700
    assert bold.italic is False

    italic = read_faces(fonts_dir / "TestSans-Italic.ttf")[0]
    assert italic.italic is True

    bold_italic = read_faces(fonts_dir / "TestSans-BoldItalic.ttf")[0]
    assert bold_italic.weight == 700
    assert bold_italic.italic is True


def test_read_faces_ttc_has_two_faces(fonts_dir: Path) -> None:
    faces = read_faces(fonts_dir / "TestCollection.ttc")
    assert len(faces) == 2
    families = {face.family for face in faces}
    assert families == {"Test Collection One", "Test Collection Two"}
    assert {face.index for face in faces} == {0, 1}


def test_read_faces_restricted_not_embeddable(fonts_dir: Path) -> None:
    face = read_faces(fonts_dir / "TestRestricted-Regular.ttf")[0]
    assert face.embeddable is False
    assert "restrict" in face.reason


def test_read_faces_cyrillic_script_detected(fonts_dir: Path) -> None:
    face = read_faces(fonts_dir / "TestCyrillic-Regular.ttf")[0]
    assert "cyrillic" in face.scripts
    assert "latin" in face.scripts
    assert "cjk" not in face.scripts


def test_read_faces_garbage_file_returns_empty(fonts_dir: Path) -> None:
    assert read_faces(fonts_dir / "Garbage.ttf") == []


def test_read_faces_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_faces(tmp_path / "nope.ttf") == []


def test_read_faces_skips_unsupported_suffixes(tmp_path: Path) -> None:
    bogus = tmp_path / "old.fon"
    bogus.write_bytes(b"whatever")
    assert read_faces(bogus) == []


# -- system_font_dirs ------------------------------------------------------------------------------


def test_system_font_dirs_returns_existing_dirs() -> None:
    dirs = system_font_dirs()
    assert isinstance(dirs, list)
    for d in dirs:
        assert d.is_dir()


# -- catalog: scan, families, find -----------------------------------------------------------------


def test_catalog_scan_builds_families_and_faces(fonts_dir: Path) -> None:
    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])
    families = catalog.families()
    assert "Test Sans" in families
    assert "Test Collection One" in families
    assert families == sorted(families, key=str.casefold)

    faces = catalog.faces_of("Test Sans")
    assert len(faces) == 4


def test_catalog_find_best_match(fonts_dir: Path) -> None:
    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])

    regular = catalog.find("Test Sans", bold=False, italic=False)
    assert regular is not None and regular.style == "Regular"

    bold = catalog.find("Test Sans", bold=True, italic=False)
    assert bold is not None and bold.weight >= 600

    italic = catalog.find("Test Sans", bold=False, italic=True)
    assert italic is not None and italic.italic is True

    bold_italic = catalog.find("Test Sans", bold=True, italic=True)
    assert bold_italic is not None and bold_italic.weight >= 600 and bold_italic.italic is True

    assert catalog.find("Nonexistent Family") is None


def test_catalog_face_for_ref(fonts_dir: Path) -> None:
    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])
    path = str(fonts_dir / "TestCollection.ttc")
    ref = FontRef.file(path, index=1)
    face = catalog.face_for(ref)
    assert face is not None
    assert face.index == 1
    assert face.family == "Test Collection Two"

    assert catalog.face_for(FontRef.standard("Helvetica")) is None
    assert catalog.face_for(FontRef.file("/no/such/path.ttf")) is None


# -- caching -----------------------------------------------------------------------------------


def test_cache_reused_on_second_scan(fonts_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    original = fonts_service.read_faces

    def counting(path: Path) -> list:  # type: ignore[type-arg]
        calls.append(path)
        return original(path)

    monkeypatch.setattr(fonts_service, "read_faces", counting)

    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])
    assert len(calls) > 0
    first_count = len(calls)

    catalog2 = FontCatalog()
    catalog2.scan(dirs=[fonts_dir])
    assert len(calls) == first_count  # no new reads: the cache satisfied the second scan
    assert catalog2.families() == catalog.families()


def test_cache_invalidated_on_mtime_change(
    fonts_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = fonts_dir / "TestSans-Regular.ttf"
    work = tmp_path / "scan"
    work.mkdir()
    target = work / "TestSans-Regular.ttf"
    target.write_bytes(src.read_bytes())

    catalog = FontCatalog()
    catalog.scan(dirs=[work])
    assert catalog.families() == ["Test Sans"]

    # modify the file (different bytes => different size) and bump mtime
    target.write_bytes(src.read_bytes() + b"\x00" * 4)
    future = time.time() + 5
    os.utime(target, (future, future))

    calls: list[Path] = []
    original = fonts_service.read_faces

    def counting(path: Path) -> list:  # type: ignore[type-arg]
        calls.append(path)
        return original(path)

    monkeypatch.setattr(fonts_service, "read_faces", counting)
    catalog2 = FontCatalog()
    catalog2.scan(dirs=[work])
    assert len(calls) == 1


def test_cache_drops_removed_files(fonts_dir: Path, tmp_path: Path) -> None:
    work = tmp_path / "scan"
    work.mkdir()
    kept = work / "TestSans-Regular.ttf"
    removed = work / "TestSans-Bold.ttf"
    kept.write_bytes((fonts_dir / "TestSans-Regular.ttf").read_bytes())
    removed.write_bytes((fonts_dir / "TestSans-Bold.ttf").read_bytes())

    catalog = FontCatalog()
    catalog.scan(dirs=[work])
    assert len(catalog.faces()) == 2

    removed.unlink()
    catalog2 = FontCatalog()
    catalog2.scan(dirs=[work])
    assert len(catalog2.faces()) == 1


def test_scan_is_cancellable(fonts_dir: Path) -> None:
    token = CancelToken()
    token.cancel()
    catalog = FontCatalog()
    with pytest.raises(Cancelled):
        catalog.scan(dirs=[fonts_dir], token=token)


def test_load_cache_reads_last_scan_without_filesystem_access(
    fonts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])

    def boom(path: Path) -> list:  # type: ignore[type-arg]
        raise AssertionError("load_cache must not read font files")

    monkeypatch.setattr(fonts_service, "read_faces", boom)

    loaded = FontCatalog()
    loaded.load_cache()
    assert loaded.families() == catalog.families()


def test_load_cache_missing_file_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDFEDITOR_DATA_DIR", str(tmp_path / "nope"))
    catalog = FontCatalog()
    catalog.load_cache()
    assert catalog.faces() == []
    assert catalog.families() == []


def test_cached_catalog_helper_loads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fonts_service, "_shared_catalog", None)
    first = fonts_service.cached_catalog()
    second = fonts_service.cached_catalog()
    assert first is second


# -- performance: cache load of ~1000 faces should be fast ----------------------------------------


def test_cache_load_performance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "perf"
    monkeypatch.setenv("PDFEDITOR_DATA_DIR", str(data_dir))
    data_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, dict[str, object]] = {}
    for i in range(1000):
        files[f"/fonts/Font{i}.ttf"] = {
            "size": 1000 + i,
            "mtime": 1700000000.0 + i,
            "faces": [
                {
                    "family": f"Family {i}",
                    "style": "Regular",
                    "weight": 400,
                    "italic": False,
                    "index": 0,
                    "embeddable": True,
                    "reason": "",
                    "scripts": ["latin"],
                    "variable": False,
                }
            ],
        }
    (data_dir / "fonts.json").write_text(
        json.dumps({"version": fonts_service.CACHE_VERSION, "files": files}), encoding="utf-8"
    )

    catalog = FontCatalog()
    start = time.perf_counter()
    catalog.load_cache()
    elapsed = time.perf_counter() - start
    assert len(catalog.faces()) == 1000
    assert elapsed < 0.1


# -- export's fsType check shares the constants here -------------------------------------------


def test_export_fonts_reuses_shared_fstype_constants() -> None:
    from pdfeditor.services.export import fonts as export_fonts

    assert export_fonts._RESTRICTED is fonts_service.RESTRICTED
    assert export_fonts._BITMAP_ONLY is fonts_service.BITMAP_ONLY


# -- text_scripts -----------------------------------------------------------------------------


def test_text_scripts_latin_only() -> None:
    assert text_scripts("Hello world 123") == {"latin"}


def test_text_scripts_detects_cyrillic() -> None:
    assert "cyrillic" in text_scripts("Привет")


def test_text_scripts_detects_cjk() -> None:
    assert "cjk" in text_scripts("中文")


def test_text_scripts_mixed() -> None:
    scripts = text_scripts("Hello Привет")
    assert {"latin", "cyrillic"} <= scripts


def test_text_scripts_empty_string() -> None:
    assert text_scripts("") == frozenset()


# -- normalize_font_name ------------------------------------------------------------------------


def test_normalize_font_name_plain() -> None:
    assert normalize_font_name("Calibri") == ("Calibri", False, False)


def test_normalize_font_name_strips_subset_prefix() -> None:
    assert normalize_font_name("ABCDEF+Calibri") == ("Calibri", False, False)


def test_normalize_font_name_bold_suffix() -> None:
    assert normalize_font_name("ABCDEF+Calibri-Bold") == ("Calibri", True, False)


def test_normalize_font_name_italic_suffix() -> None:
    assert normalize_font_name("Calibri-Italic") == ("Calibri", False, True)


def test_normalize_font_name_bold_italic_suffix() -> None:
    assert normalize_font_name("Calibri-BoldItalic") == ("Calibri", True, True)
    assert normalize_font_name("Calibri,BoldItalic") == ("Calibri", True, True)


def test_normalize_font_name_oblique_suffix() -> None:
    family, bold, italic = normalize_font_name("Arial-Oblique")
    assert family == "Arial"
    assert italic is True
    assert bold is False


# -- installed_ref_for_font_name ----------------------------------------------------------------


def test_installed_ref_for_font_name_matches_normalized_family(fonts_dir: Path) -> None:
    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])
    ref = installed_ref_for_font_name(catalog, "ABCDEF+Test Sans-Bold")
    assert ref is not None
    assert ref.name == "Test Sans"
    face = catalog.face_for(ref)
    assert face is not None
    assert face.weight >= 600


def test_installed_ref_for_font_name_unknown_family_returns_none(fonts_dir: Path) -> None:
    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])
    assert installed_ref_for_font_name(catalog, "Nonexistent Family XYZ") is None


def test_installed_ref_for_font_name_restricted_returns_none(fonts_dir: Path) -> None:
    catalog = FontCatalog()
    catalog.scan(dirs=[fonts_dir])
    assert installed_ref_for_font_name(catalog, "Test Restricted") is None
