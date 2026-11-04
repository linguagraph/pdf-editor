from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "generated"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-goldens",
        action="store_true",
        help="rewrite reference images (tests/golden/data, tests/golden/ui) instead of comparing",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Generate the fixture corpus once if it is missing; isolate the user data folder."""
    # Logs, crash reports and recovery copies must never touch the real profile.
    os.environ["PDFEDITOR_DATA_DIR"] = tempfile.mkdtemp(prefix="pdfeditor-test-data-")
    # Same GC policy as the app: never collect MuPDF objects outside the engine lock.
    from pdfeditor.core.engine_lock import install_manual_gc

    install_manual_gc()
    if not (FIXTURES / "text_multipage.pdf").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_fixtures.py")], check=True)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def fixture_pdf(fixtures_dir: Path):
    """``fixture_pdf("images")`` -> path of tests/fixtures/generated/images.pdf."""

    def get(name: str) -> Path:
        path = fixtures_dir / f"{name.removesuffix('.pdf')}.pdf"
        assert path.exists(), f"missing fixture {path}"
        return path

    return get


@pytest.fixture(autouse=True)
def _collect_garbage_under_engine_lock():
    yield
    from pdfeditor.core.engine_lock import collect

    collect()


@pytest.fixture
def throwaway_pfx(tmp_path: Path):
    """``throwaway_pfx()`` -> a PFX file with a new self-signed test key; password "test".

    These are the only keys tests may sign with (see tests/services/test_signing_windows.py).
    """

    def make(
        kind: str = "rsa",
        name: str = "Elanero Test Signer",
        days: int = 30,
        key_usage: str = "sign",
        friendly: bytes = b"elanero-test",
    ) -> Path:
        import datetime as dt
        import uuid

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import NameOID

        key = (
            rsa.generate_private_key(65537, 2048)
            if kind == "rsa"
            else ec.generate_private_key(ec.SECP256R1())
        )
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = dt.datetime.now(dt.UTC)
        signing = key_usage == "sign"
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=max(2, -days + 2)))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(
                x509.KeyUsage(
                    signing, signing, not signing, False, False, False, False, False, False
                ),
                True,
            )
            .sign(key, hashes.SHA256())
        )
        path = tmp_path / f"throwaway-{uuid.uuid4().hex[:8]}.pfx"
        path.write_bytes(
            pkcs12.serialize_key_and_certificates(
                friendly, key, cert, None, serialization.BestAvailableEncryption(b"test")
            )
        )
        return path

    return make


@pytest.fixture
def signature_status():
    """``signature_status(path)`` -> pyHanko's validation status of every signature in a PDF,
    each with its own certificate as trust root (the throwaway ones are self-signed)."""

    def statuses(path: Path, password: str | None = None) -> list:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation import validate_pdf_signature
        from pyhanko_certvalidator import ValidationContext

        with path.open("rb") as fh:
            reader = PdfFileReader(fh, strict=False)
            if reader.encrypted:
                reader.decrypt(password or "")
            return [
                validate_pdf_signature(s, ValidationContext(trust_roots=[s.signer_cert]))
                for s in reader.embedded_signatures
            ]

    return statuses
