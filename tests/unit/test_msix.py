"""The Microsoft Store MSIX package: manifest, identity, version and assets (Phase 18 S2/S3)."""

from __future__ import annotations

import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

from pdfeditor import __version__

ROOT = Path(__file__).resolve().parents[2]
NS = {
    "m": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
    "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
    "uap3": "http://schemas.microsoft.com/appx/manifest/uap/windows10/3",
    "desktop": "http://schemas.microsoft.com/appx/manifest/desktop/windows10",
    "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
}


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def msix():
    return load("msix_package")


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.1.0", "0.1.0.0"), ("1.2", "1.2.0.0"), ("2.10.3rc1", "2.10.3.0"), ("3", "3.0.0.0")],
)
def test_store_version_has_a_zero_fourth_part(msix, version: str, expected: str) -> None:
    assert msix.msix_version(version) == expected


def test_manifest_is_valid_and_complete(msix) -> None:
    identity = msix.Identity("Test.Name", 'CN="A & B", O=X', "Pub & Co", reserved=True)
    root = ET.fromstring(msix.manifest_text(identity, __version__))  # well-formed XML
    ident = root.find("m:Identity", NS)
    assert ident is not None
    assert ident.get("Name") == "Test.Name"
    assert ident.get("Publisher") == 'CN="A & B", O=X'  # escaped, then read back intact
    assert ident.get("Version") == msix.msix_version(__version__)
    assert ident.get("ProcessorArchitecture") == "x64"
    assert root.findtext("m:Properties/m:PublisherDisplayName", namespaces=NS) == "Pub & Co"
    family = root.find("m:Dependencies/m:TargetDeviceFamily", NS)
    assert family is not None and family.get("Name") == "Windows.Desktop"
    assert family.get("MinVersion") == "10.0.17763.0"
    app = root.find("m:Applications/m:Application", NS)
    assert app is not None
    assert app.get("Executable") == "Elanero.exe"
    assert app.get("EntryPoint") == "Windows.FullTrustApplication"
    capabilities = [c.get("Name") for c in root.iterfind("m:Capabilities/rescap:Capability", NS)]
    assert capabilities == ["runFullTrust"]
    types = [t.text for t in root.iter(f"{{{NS['uap']}}}FileType")]
    assert types == [".pdf"]
    alias = root.find(".//desktop:ExecutionAlias", NS)
    assert alias is not None and alias.get("Alias") == "Elanero.exe"
    assert "@" not in ET.tostring(root, encoding="unicode")  # every placeholder filled


def test_every_referenced_asset_is_made(msix, tmp_path: Path) -> None:
    identity = msix.load_identity()
    root = ET.fromstring(msix.manifest_text(identity, __version__))
    referenced: set[str] = set()
    for element in root.iter():
        for value in [*element.attrib.values(), element.text or ""]:
            if value.startswith("Assets\\"):
                referenced.add(value.removeprefix("Assets\\").removesuffix(".png"))
    written = {p.name for p in msix.write_assets(tmp_path)}
    for base in referenced:
        # Windows resolves "X.png" to a scale- or targetsize- qualified file.
        assert any(name.startswith(base + ".scale-") for name in written), base
    assert any(".targetsize-256_altform-unplated" in name for name in written)


def test_assets_are_the_icon_scaled_not_redrawn(msix, tmp_path: Path) -> None:
    msix.write_assets(tmp_path)
    for asset in msix.assets():
        with Image.open(tmp_path / asset.name) as image:
            assert image.size == asset.canvas, asset.name
            assert image.mode == "RGBA"
    # A shell icon at a size the pack has is that exact PNG.
    pack = ROOT / "src/pdfeditor/data/branding/elanero/icons/png/elanero-48.png"
    with (
        Image.open(tmp_path / "Square44x44Logo.targetsize-48.png") as made,
        Image.open(pack) as orig,
    ):
        assert made.convert("RGBA").tobytes() == orig.convert("RGBA").tobytes()
    # Tiles leave room around the icon: their corners are transparent.
    with Image.open(tmp_path / "Wide310x150Logo.scale-100.png") as wide:
        assert wide.getpixel((0, 0))[3] == 0 and wide.getpixel((309, 149))[3] == 0


def test_identity_example_has_every_field(msix) -> None:
    path = ROOT / "packaging/msix/identity.example.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"name", "publisher", "publisher_display_name", "reserved"} <= data.keys()
    identity = msix.load_identity(path)
    assert identity.publisher.startswith("CN=") and not identity.reserved


def test_the_real_identity_stays_out_of_git() -> None:
    import subprocess

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "packaging/msix/identity.json"], cwd=ROOT
    )
    assert ignored.returncode == 0


def test_build_uses_the_example_without_a_real_identity(msix, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(msix, "IDENTITY", tmp_path / "missing.json")
    assert not msix.load_identity().reserved


def test_build_script_paths(monkeypatch) -> None:
    b = load("build_exe")
    assert b.exe_path(onedir=True).parent.name == "Elanero"
    assert b.exe_path().parent == b.DIST
    assert b.msix_path().name == f"Elanero-{__version__}-x64.msix"


def test_a_short_package_is_caught(msix, tmp_path: Path) -> None:
    import zipfile

    layout = tmp_path / "layout"
    (layout / "_internal" / "Py Side").mkdir(parents=True)
    for name in ("Elanero.exe", "_internal/Py Side/Qt6Core.dll", "_internal/lib.pyd"):
        (layout / name).write_bytes(b"x")
    package = tmp_path / "app.msix"
    with zipfile.ZipFile(package, "w") as archive:  # makeappx percent-encodes names
        for name in ("[Content_Types].xml", "AppxBlockMap.xml", "Elanero.exe"):
            archive.writestr(name, b"x")
        archive.writestr("_internal/Py%20Side/Qt6Core.dll", b"x")
    assert msix.missing_files(layout, package) == ["_internal/lib.pyd"]


def test_only_the_branding_the_app_loads_is_bundled() -> None:
    spec = (ROOT / "packaging" / "pdfeditor.spec").read_text(encoding="utf-8")
    assert "BRANDING_KEEP" in spec and '"/splash/png/"' in spec
