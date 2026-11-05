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


def test_request_email_and_package_import_load_no_signing_libraries() -> None:
    """The request module is pyHanko-based, so the package must import it only when preparing;
    the email draft is plain text and must not load anything either."""
    code = (
        "import sys\n"
        "import pdfeditor.services.signing as signing\n"
        "from pdfeditor.model.geometry import Rect\n"
        "from pdfeditor.model.signing import SignatureRequest\n"
        "signing.mailto_url([SignatureRequest('A', 'a@x.org', 0, Rect(0, 0, 1, 1))], 'd.pdf')\n"
        f"loaded = sorted(m for m in {HEAVY!r} if m in sys.modules)\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_appearance_mark_needs_only_pillow() -> None:
    """The logo helper is imported with the signing code, but it must not pull in pyHanko."""
    code = (
        "import sys\n"
        "import pdfeditor.services.signing.appearance_mark\n"
        f"loaded = sorted(m for m in {HEAVY!r} if m in sys.modules)\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
