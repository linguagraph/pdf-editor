# PyInstaller spec for the self-contained one-file pdfeditor executable.
# Build with:  uv run python scripts/build_exe.py   (wraps: pyinstaller packaging/pdfeditor.spec)
# ruff: noqa
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
GENERATED = ROOT / "build" / "generated"
GENERATED.mkdir(parents=True, exist_ok=True)

VERSION = re.search(
    r'__version__ = "([^"]+)"', (ROOT / "src" / "pdfeditor" / "__init__.py").read_text()
).group(1)
numbers = [int(x) for x in re.findall(r"\d+", VERSION)[:3]] + [0]
while len(numbers) < 4:
    numbers.append(0)
VERSION_TUPLE = tuple(numbers[:4])

# The approved Elanero icon (16-256 px), used as shipped: no conversion.
BRAND = ROOT / "src" / "pdfeditor" / "data" / "branding" / "elanero"
ICON = BRAND / "icons" / "platform" / "elanero.ico"

# Windows version resource.
VERSION_FILE = GENERATED / "version_info.txt"
VERSION_FILE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={VERSION_TUPLE}, prodvers={VERSION_TUPLE}),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'pdfeditor'),
      StringStruct('FileDescription', 'Elanero - PDF Editor'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'pdfeditor'),
      StringStruct('LegalCopyright', 'AGPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'Elanero.exe'),
      StringStruct('ProductName', 'Elanero - PDF Editor'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
    encoding="utf-8",
)

# Qt modules the app doesn't use: excluding them keeps their DLLs and plugins out of the exe.
QT_EXCLUDES = [
    f"PySide6.{m}"
    for m in (
        "Qt3DAnimation Qt3DCore Qt3DExtras Qt3DInput Qt3DLogic Qt3DRender QtBluetooth QtCharts "
        "QtConcurrent QtDataVisualization QtDesigner QtGraphs QtHelp QtHttpServer QtLocation "
        "QtMultimedia QtMultimediaWidgets QtNetworkAuth QtNfc QtOpenGL QtOpenGLWidgets QtPdf "
        "QtPdfWidgets QtPositioning QtQml QtQuick QtQuick3D QtQuickControls2 QtQuickWidgets "
        "QtRemoteObjects QtScxml QtSensors QtSerialBus QtSerialPort QtSpatialAudio QtSql "
        "QtStateMachine QtTest QtTextToSpeech QtUiTools QtWebChannel QtWebEngineCore "
        "QtWebEngineQuick QtWebEngineWidgets QtWebSockets QtWebView QtXml"
    ).split()
]
# Dev/test-only packages that must never end up in the product. numpy is not a runtime
# dependency (the compare and OCR code use Pillow); keep it out if a tool pulls it in.
DEV_EXCLUDES = [
    "pytest", "_pytest", "hypothesis", "pytestqt", "pytest_benchmark", "mypy", "ruff",
    "importlinter", "grimp", "pre_commit", "tkinter", "unittest", "pydoc_data", "numpy",
    "pypdfium2",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    # Of the branding pack only what the app loads: window icon PNGs, the .ico (file
    # associations), the splash PNGs and the wordmark. The SVG/ICNS sources stay in the repo.
    datas=collect_data_files(
        "pdfeditor",
        includes=[
            "data/*", "data/icons/*", "data/tessdata/*",
            "data/branding/elanero/icons/png/*", "data/branding/elanero/icons/platform/elanero.ico",
            "data/branding/elanero/splash/png/*", "data/branding/elanero/branding/*",
            "data/branding/elanero/README.txt",
        ],
    )
    # license texts written by scripts/build_exe.py (AGPL notice + third-party notices)
    + [(str(GENERATED / "THIRD_PARTY_NOTICES.txt"), "pdfeditor/data"),
       (str(ROOT / "LICENSE"), "pdfeditor/data")],
    # Loaded lazily through the engine registry, so static analysis can't see it.
    hiddenimports=["pdfeditor.engine.mupdf"],
    excludes=QT_EXCLUDES + DEV_EXCLUDES,
    noarchive=False,
)
# Qt pieces nothing in the app loads, pulled in by plugins or copied wholesale: the software
# OpenGL fallback (20 MB), the virtual keyboard (which drags in QML/Quick/OpenGL), the PDF
# image plugin (drags in Qt6Pdf), touch/TLS/network-info plugins, the Direct2D platform.
# `--self-test` and the windowed launch check in scripts/build_exe.py prove what remains.
DROP_FILES = {
    "opengl32sw.dll",
    "qt6virtualkeyboard.dll", "qtvirtualkeyboardplugin.dll",
    "qt6qml.dll", "qt6qmlmodels.dll", "qt6qmlmeta.dll", "qt6qmlworkerscript.dll",
    "qt6quick.dll", "qt6opengl.dll",
    "qt6pdf.dll", "qpdf.dll",
    "qtuiotouchplugin.dll", "qdirect2d.dll", "qminimal.dll",
    "qnetworklistmanager.dll", "qopensslbackend.dll", "qschannelbackend.dll",
    "qcertonlybackend.dll",
}


# Of the branding pack, only what the app loads (the pattern list above also lets through the
# pack's own documents and sources, and the folder build picks up whole directories).
BRANDING_KEEP = (
    "/icons/png/", "/icons/platform/elanero.ico", "/splash/png/", "/elanero/branding/",
    "/elanero/readme.txt",
)


def _keep(entry):
    name = entry[0].replace("\\", "/").lower()
    base = name.rsplit("/", 1)[-1]
    if base in DROP_FILES:
        return False
    if "/branding/elanero/" in name and not any(k in name for k in BRANDING_KEEP):
        return False
    # Qt's own UI strings: keep qtbase/qt catalogues, drop Qt Help/Designer/etc.
    if "/translations/" in name and not base.startswith(("qtbase_", "qt_")) or "qt_help_" in base:
        return False
    return True


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]
pyz = PYZ(a.pure)
# PDFEDITOR_ONEDIR=1 (build_exe.py --onedir / --msix) builds the same app as a folder,
# dist/Elanero/Elanero.exe + _internal/, for the MSIX package: it starts without unpacking
# 70 MB to %TEMP% first. The default is the portable one-file dist/Elanero.exe.
ONEDIR = os.environ.get("PDFEDITOR_ONEDIR") == "1"
exe = EXE(
    pyz,
    a.scripts,
    *([] if ONEDIR else [a.binaries, a.datas]),
    [],
    exclude_binaries=ONEDIR,
    name="Elanero",
    icon=str(ICON),
    version=str(VERSION_FILE),
    console=False,
    upx=False,  # UPX-packed Qt DLLs trigger antivirus false positives and slow startup
    runtime_tmpdir=None,
)
if ONEDIR:
    coll = COLLECT(exe, a.binaries, a.datas, name="Elanero", upx=False)
