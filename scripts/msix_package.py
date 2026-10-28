"""Build the Microsoft Store MSIX package from the one-folder build (PLAN.md Phase 18, S3).

Used by ``scripts/build_exe.py --msix``. The steps:

1. Copy ``dist/Elanero/`` (the ``--onedir`` build) into a package layout.
2. Write ``AppxManifest.xml`` from ``packaging/msix/`` with the identity from Partner Center
   (``identity.json``, kept out of git; ``identity.example.json`` has test values) and the app
   version as ``X.Y.Z.0``.
3. Make the tile and icon assets: scaled copies of the Elanero icon, never redrawn.
4. Index them into ``resources.pri`` (makepri), so Windows picks the right scale.
5. Pack with makeappx. The Store signs the package itself; ``--sign-test`` signs it with a
   self-signed certificate for installing on a test machine instead.

The Windows SDK tools (makeappx, makepri, signtool) are needed only here, at build time.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MSIX_DIR = ROOT / "packaging" / "msix"
# The real identity is kept out of git; the example (test values) stands in for it.
IDENTITY = MSIX_DIR / "identity.json"
IDENTITY_EXAMPLE = MSIX_DIR / "identity.example.json"
TEMPLATE = MSIX_DIR / "AppxManifest.xml"
ICONS = ROOT / "src" / "pdfeditor" / "data" / "branding" / "elanero" / "icons" / "png"
MASTER = ROOT / "src" / "pdfeditor" / "data" / "branding" / "elanero" / "source"
BUILD = ROOT / "build" / "msix"
SDK_BIN_GLOB = "Program Files (x86)/Windows Kits/10/bin/*/x64/{tool}.exe"

SCALES = (100, 125, 150, 200, 400)
# Shell icon sizes (taskbar, Start, Explorer). The pack has a hand-made PNG for each.
TARGET_SIZES = (16, 24, 32, 48, 256)
# Tiles show the icon with some room around it, like other apps' tiles.
TILE_ICON_SHARE = 0.66
# For --sign-test only: a throwaway certificate, so its password isn't a secret.
TEST_PFX_PASSWORD = "elanero-test-only"


@dataclass(frozen=True)
class Identity:
    name: str
    publisher: str
    publisher_display_name: str
    reserved: bool


def load_identity(path: Path | None = None) -> Identity:
    if path is None:
        path = IDENTITY if IDENTITY.exists() else IDENTITY_EXAMPLE
    data = json.loads(path.read_text(encoding="utf-8"))
    return Identity(
        name=data["name"],
        publisher=data["publisher"],
        publisher_display_name=data["publisher_display_name"],
        reserved=bool(data.get("reserved", False)),
    )


def msix_version(version: str) -> str:
    """``1.2.3`` as ``1.2.3.0`` (the Store requires the fourth part be 0; ``1.2`` is
    ``1.2.0.0``). Pre-releases are refused: ``1.2rc1`` has no Store version that sorts below
    the final ``1.2``, and the Store only accepts increasing versions."""
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if match is None:
        raise ValueError(f"Store packages need a final X.Y.Z version, not {version!r}")
    return ".".join(n or "0" for n in match.groups()) + ".0"


def manifest_text(identity: Identity, version: str) -> str:
    from xml.sax.saxutils import escape

    values = {
        "@NAME@": identity.name,
        "@PUBLISHER@": identity.publisher,
        "@PUBLISHER_DISPLAY_NAME@": identity.publisher_display_name,
        "@VERSION@": msix_version(version),
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(key, escape(value, {'"': "&quot;"}))
    return text


@dataclass(frozen=True)
class Asset:
    """One image file: ``canvas`` pixels with the icon ``icon`` pixels high, centered."""

    name: str
    canvas: tuple[int, int]
    icon: int


def _px(size: float) -> int:
    """Rounded half up, like Windows' asset sizes (50 at 125% is 63, not round()'s 62)."""
    return math.floor(size + 0.5)


def assets() -> list[Asset]:
    out: list[Asset] = []
    for scale in SCALES:
        k = scale / 100
        side44, side50 = _px(44 * k), _px(50 * k)
        out.append(Asset(f"Square44x44Logo.scale-{scale}.png", (side44, side44), side44))
        out.append(Asset(f"StoreLogo.scale-{scale}.png", (side50, side50), side50))
        square = _px(150 * k)
        tile = _px(square * TILE_ICON_SHARE)
        out.append(Asset(f"Square150x150Logo.scale-{scale}.png", (square, square), tile))
        out.append(Asset(f"Wide310x150Logo.scale-{scale}.png", (_px(310 * k), square), tile))
    for size in TARGET_SIZES:
        for suffix in ("", "_altform-unplated"):
            out.append(Asset(f"Square44x44Logo.targetsize-{size}{suffix}.png", (size, size), size))
    return out


def _icon(size: int) -> Image.Image:
    """The icon at ``size`` px: the pack's own PNG when it has that size, else the master
    scaled down (Lanczos)."""
    exact = ICONS / f"elanero-{size}.png"
    if exact.exists():
        return Image.open(exact).convert("RGBA")
    master = Image.open(MASTER / "elanero-master-1024.png").convert("RGBA")
    return master.resize((size, size), Image.Resampling.LANCZOS)


def write_assets(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for asset in assets():
        canvas = Image.new("RGBA", asset.canvas, (0, 0, 0, 0))
        icon = _icon(asset.icon)
        canvas.paste(
            icon, ((asset.canvas[0] - asset.icon) // 2, (asset.canvas[1] - asset.icon) // 2)
        )
        path = folder / asset.name
        canvas.save(path)
        written.append(path)
    return written


def _output(result: subprocess.CompletedProcess[bytes]) -> str:
    """A tool's output as text: makepri writes UTF-16, the others the console code page."""
    raw = result.stdout + result.stderr
    if raw[1:2] == b"\0":
        text = raw.decode("utf-16-le", "replace")
    else:
        text = raw.decode("mbcs" if os.name == "nt" else "utf-8", "replace")
    return text.strip()[-2000:]


def run_tool(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run an SDK tool quietly, but stop with its own output if it fails."""
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        name = Path(command[0]).name
        raise SystemExit(f"{name} failed ({result.returncode}):\n{_output(result)}")
    return result


def find_sdk_tool(tool: str) -> Path:
    found = shutil.which(tool)
    if found:
        return Path(found)
    drive = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    matches = sorted(drive.glob(SDK_BIN_GLOB.format(tool=tool)))
    if not matches:
        raise SystemExit(f"{tool}.exe not found: install the Windows 10/11 SDK")
    return matches[-1]


def make_layout(app_dir: Path, layout: Path, identity: Identity, version: str) -> Path:
    """The package's file tree: the one-folder app, the manifest, assets and resources.pri."""
    if not (app_dir / "Elanero.exe").exists():
        raise SystemExit(f"{app_dir} has no Elanero.exe: build with --onedir first")
    shutil.rmtree(layout, ignore_errors=True)
    shutil.copytree(app_dir, layout)
    manifest = manifest_text(identity, version)
    (layout / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    # Index only the manifest and its assets into resources.pri, not the whole app folder.
    pri_root = layout.parent / "pri"
    shutil.rmtree(pri_root, ignore_errors=True)
    write_assets(pri_root / "Assets")
    (pri_root / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    makepri = find_sdk_tool("makepri")
    config = layout.parent / "priconfig.xml"
    run_tool(
        [str(makepri), "createconfig", "/cf", str(config), "/dq", "en-US", "/pv", "10.0.0", "/o"]
    )
    # One resources.pri with every scale: the default config splits scales and languages into
    # resources.scale-*.pri for bundles, and a single package registered that way has no
    # default tile image ("no default or neutral resource").
    text = config.read_text(encoding="utf-8")
    config.write_text(re.sub(r"\s*<packaging>.*?</packaging>", "", text, flags=re.DOTALL))
    run_tool(
        [
            str(makepri),
            "new",
            "/pr",
            str(pri_root),
            "/cf",
            str(config),
            "/mn",
            str(pri_root / "AppxManifest.xml"),
            "/of",
            str(layout / "resources.pri"),
            "/o",
        ]
    )
    shutil.copytree(pri_root / "Assets", layout / "Assets")
    return layout


# Written by makeappx itself, not taken from the layout.
PACKAGE_METADATA = {"[Content_Types].xml", "AppxBlockMap.xml", "AppxSignature.p7x"}
PACK_ATTEMPTS = 4
PACK_RETRY_DELAY = 10  # seconds, doubled after each failed attempt


def missing_files(layout: Path, package: Path) -> list[str]:
    """Layout files that aren't in ``package`` (names in the zip are percent-encoded)."""
    import zipfile
    from urllib.parse import unquote

    with zipfile.ZipFile(package) as archive:
        packed = {unquote(n).casefold() for n in archive.namelist()} - {
            m.casefold() for m in PACKAGE_METADATA
        }
    files = (p.relative_to(layout).as_posix() for p in layout.rglob("*") if p.is_file())
    return sorted(f for f in files if f.casefold() not in packed)


def pack(layout: Path, out: Path) -> Path:
    """Pack ``layout`` into ``out`` and check every file made it in.

    Right after the layout is copied, makeappx has reported success while leaving out over a
    hundred of the app's DLLs, or failed outright; a minute later the same layout packs
    completely (likely antivirus still scanning the new files). So a short or failed pack is
    retried after a growing pause, and a short package is never returned.
    """
    import time

    makeappx = find_sdk_tool("makeappx")
    out.parent.mkdir(parents=True, exist_ok=True)
    problem = ""
    for attempt in range(1, PACK_ATTEMPTS + 1):
        if attempt > 1:
            delay = PACK_RETRY_DELAY * 2 ** (attempt - 2)
            print(f"makeappx: {problem}; retrying in {delay}s (attempt {attempt})")
            time.sleep(delay)
        result = subprocess.run(
            [str(makeappx), "pack", "/d", str(layout), "/p", str(out), "/o"],
            capture_output=True,
        )
        if result.returncode != 0:
            problem = f"exit code {result.returncode}: {_output(result)}"
            continue
        missing = missing_files(layout, out)
        if not missing:
            return out
        problem = f"{len(missing)} file(s) left out, e.g. {missing[0]}"
    out.unlink(missing_ok=True)
    raise SystemExit(f"couldn't pack a complete MSIX: {problem}")


def test_certificate(publisher: str, folder: Path = BUILD) -> Path:
    """A self-signed code-signing certificate whose subject is ``publisher`` (as the
    manifest requires), exported as a .pfx. Kept in the user's certificate store and reused."""
    pfx = folder / "test-signing.pfx"
    folder.mkdir(parents=True, exist_ok=True)
    subject = publisher.replace("'", "''")  # a PowerShell single-quoted string
    script = f"""
$ErrorActionPreference = 'Stop'
$cert = Get-ChildItem Cert:\\CurrentUser\\My | Where-Object {{
    $_.Subject -eq '{subject}' -and $_.NotAfter -gt (Get-Date).AddDays(7) -and
    $_.EnhancedKeyUsageList.ObjectId -contains '1.3.6.1.5.5.7.3.3' }} |
    Select-Object -First 1
if (-not $cert) {{
    $cert = New-SelfSignedCertificate -Type Custom -Subject '{subject}' `
        -KeyUsage DigitalSignature -FriendlyName 'Elanero MSIX test signing' `
        -CertStoreLocation Cert:\\CurrentUser\\My `
        -TextExtension @('2.5.29.37={{text}}1.3.6.1.5.5.7.3.3', '2.5.29.19={{text}}')
}}
$password = ConvertTo-SecureString -String '{TEST_PFX_PASSWORD}' -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath '{pfx}' -Password $password | Out-Null
"""
    # Started from PowerShell 7, Windows PowerShell inherits its PSModulePath and then can't
    # load its own PKI module; let it find its defaults.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(f"couldn't make the test certificate:\n{_output(result)}")
    return pfx


def sign_test(package: Path, publisher: str) -> Path:
    pfx = test_certificate(publisher)
    signtool = find_sdk_tool("signtool")
    run_tool(
        [
            str(signtool),
            "sign",
            "/fd",
            "SHA256",
            "/f",
            str(pfx),
            "/p",
            TEST_PFX_PASSWORD,
            str(package),
        ]
    )
    return pfx


def build_msix(app_dir: Path, out: Path, version: str, *, sign: bool = False) -> Path:
    identity = load_identity()
    if not identity.reserved:
        print(
            "note: building with the test identity; for the Store, put the Partner Center "
            "values in packaging/msix/identity.json (see identity.example.json)"
        )
    layout = make_layout(app_dir, BUILD / "layout", identity, version)
    pack(layout, out)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"built {out} ({size_mb:.1f} MB)")
    if sign:
        pfx = sign_test(out, identity.publisher)
        print(f"signed {out.name} with the test certificate {pfx}")
    return out
