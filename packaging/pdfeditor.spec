# PyInstaller spec for the self-contained one-file pdfeditor executable.
# Build with:  uv run python scripts/build_exe.py   (wraps: pyinstaller packaging/pdfeditor.spec)
# ruff: noqa
import re
from pathlib import Path

from PIL import Image
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

# Windows icon (multi-size) from the bundled PNG.
ICON = GENERATED / "pdfeditor.ico"
Image.open(ROOT / "src" / "pdfeditor" / "data" / "icon.png").save(
    ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
)

# Windows version resource.
VERSION_FILE = GENERATED / "version_info.txt"
VERSION_FILE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={VERSION_TUPLE}, prodvers={VERSION_TUPLE}),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'pdfeditor'),
      StringStruct('FileDescription', 'pdfeditor PDF editor'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'pdfeditor'),
      StringStruct('LegalCopyright', 'AGPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'pdfeditor.exe'),
      StringStruct('ProductName', 'pdfeditor'),
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
    datas=collect_data_files("pdfeditor", includes=["data/*", "data/tessdata/*"])
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


def _keep(entry):
    name = entry[0].replace("\\", "/").lower()
    base = name.rsplit("/", 1)[-1]
    if base in DROP_FILES:
        return False
    # Qt's own UI strings: keep qtbase/qt catalogues, drop Qt Help/Designer/etc.
    if "/translations/" in name and not base.startswith(("qtbase_", "qt_")) or "qt_help_" in base:
        return False
    return True


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pdfeditor",
    icon=str(ICON),
    version=str(VERSION_FILE),
    console=False,
    upx=False,  # UPX-packed Qt DLLs trigger antivirus false positives and slow startup
    runtime_tmpdir=None,
)
