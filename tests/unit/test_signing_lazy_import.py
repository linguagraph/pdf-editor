"""Startup speed: signing's heavy libraries must not load with the main window."""

from __future__ import annotations

import os
import subprocess
import sys

HEAVY = (
    "pyhanko",
    "pyhanko_certvalidator",
    "cryptography",
    "pkcs11",
    "asn1crypto",
    "aiohttp",
    "lxml",
    "pdfeditor.services.signing.inspect",  # the Signatures panel's checker: only when it's used
    "pdfeditor.services.signing._pdf",
)


def test_main_window_import_does_not_load_signing_libraries() -> None:
    code = (
        "import sys\n"
        "import pdfeditor.ui.main_window\n"
        f"loaded = sorted(m for m in {HEAVY!r} if m in sys.modules)\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
