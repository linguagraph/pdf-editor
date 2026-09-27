"""Create ``tests/fixtures/real/signed_selfsigned.pdf``: a PDF signed by pyHanko.

Redistributable signed PDFs are hard to find, so the corpus carries one we sign ourselves with a
throwaway self-signed certificate (generated here, never stored). The output is committed; run
this script again only to replace it. pyHanko is a dev-only dependency (``.[dev]``).

    uv run python scripts/make_signed_sample.py
"""

from __future__ import annotations

import datetime as dt
import io
import tempfile
from pathlib import Path

import pymupdf
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import fields, signers

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "real" / "signed_selfsigned.pdf"

BODY = (
    "This agreement is a test document for the PDF editor.\n"
    "It carries an approval signature made with a self-signed test certificate.\n"
    "Adding comments must not invalidate the signature."
)


def base_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Signed sample agreement", fontname="hebo", fontsize=20)
    page.insert_textbox(pymupdf.Rect(72, 120, 523, 260), BODY, fontname="helv", fontsize=12)
    page.insert_text((72, 600), "Signature:", fontname="helv", fontsize=12)
    doc.set_metadata({"title": "Signed sample agreement", "author": "pdf-editor test corpus"})
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return bytes(data)


def self_signed(tmp: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "PDF Editor Test Signer"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "pdf-editor test corpus"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365 * 20))
        .add_extension(
            x509.KeyUsage(True, True, False, False, False, False, False, False, False), True
        )
        .sign(key, hashes.SHA256())
    )
    key_file, cert_file = tmp / "key.pem", tmp / "cert.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_file, cert_file


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        key_file, cert_file = self_signed(Path(tmp))
        signer = signers.SimpleSigner.load(str(key_file), str(cert_file))
    assert signer is not None
    writer = IncrementalPdfFileWriter(io.BytesIO(base_pdf()))
    out = io.BytesIO()
    signers.sign_pdf(
        writer,
        signers.PdfSignatureMetadata(
            field_name="Signature1", reason="Approval (test signature)", md_algorithm="sha256"
        ),
        signer=signer,
        new_field_spec=fields.SigFieldSpec("Signature1", box=(150, 220, 400, 260)),
        output=out,
    )
    OUT.write_bytes(out.getvalue())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
