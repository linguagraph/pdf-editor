"""Password protection: encrypt, lift, owner unlock (contract tests over the Protocol)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.engine.base import Engine, PasswordRequired, SaveOptions
from pdfeditor.model.metadata import EncryptionMethod, Permissions, SecuritySettings

RESTRICTED = Permissions(print=False, modify=False, copy=False, annotate=True)


def test_encrypt_on_save(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    if not engine.capabilities.encrypt:
        pytest.skip("engine can't encrypt")
    doc = engine.open(fixture_pdf("report"))
    doc.set_pending_security(
        SecuritySettings(EncryptionMethod.AES_256, "open me", "boss", RESTRICTED)
    )
    assert not doc.can_save_incrementally()
    out = doc.save(tmp_path / "locked.pdf")
    assert doc.pending_security() is None
    # the open document carries on with the file it just wrote
    assert "Quarterly Report" in doc.page(0).text_page(with_chars=False).text
    doc.close()

    with pytest.raises(PasswordRequired):
        engine.open(out)
    user = engine.open(out, "open me")
    info = user.info()
    assert info.encryption is EncryptionMethod.AES_256
    assert not info.permissions.print and not info.permissions.copy and info.permissions.annotate
    assert not user.has_owner_access()
    user.close()
    owner = engine.open(out, "boss")
    assert owner.has_owner_access()
    owner.close()
    with pikepdf.open(out, password="open me") as pdf:
        assert pdf.is_encrypted and len(pdf.pages) == 2
    assert len(pdfium.PdfDocument(str(out), password="open me")) == 2


def test_aes128_without_user_password(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc = engine.open(fixture_pdf("report"))
    doc.set_pending_security(SecuritySettings(EncryptionMethod.AES_128, "", "boss", RESTRICTED))
    out = doc.save(tmp_path / "restricted.pdf")
    doc.close()
    opened = engine.open(out)  # opens without a password, but restricted
    assert opened.info().encryption is EncryptionMethod.AES_128
    assert not opened.info().permissions.print
    assert not opened.has_owner_access()
    assert opened.unlock_owner("boss") and opened.has_owner_access()
    opened.close()


def test_unlock_owner_and_remove_security(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    path = tmp_path / "enc.pdf"
    shutil.copy2(fixture_pdf("encrypted"), path)
    doc = engine.open(path, "user")
    text = doc.page(0).text_page(with_chars=False).text
    assert text.strip() and not doc.has_owner_access()
    assert not doc.unlock_owner("wrong")
    # a wrong owner password must not disturb the open document
    assert doc.page(0).text_page(with_chars=False).text == text
    assert doc.unlock_owner("owner") and doc.has_owner_access()
    doc.set_pending_security(SecuritySettings(EncryptionMethod.NONE))
    doc.save()
    doc.close()
    plain = engine.open(path)
    assert plain.info().encryption is EncryptionMethod.NONE
    assert plain.page(0).text_page(with_chars=False).text == text
    plain.close()


def test_security_kept_when_not_changed(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc = engine.open(fixture_pdf("encrypted"), "owner")
    out = doc.save(tmp_path / "copy.pdf", SaveOptions())
    doc.close()
    with pytest.raises(PasswordRequired):
        engine.open(out)
    engine.open(out, "user").close()


def test_plain_document_has_owner_access(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("report"))
    assert doc.has_owner_access() and doc.pending_security() is None
    doc.close()


def test_metadata_command_syncs_xmp(fixture_pdf) -> None:
    from pdfeditor.core.commands import SetMetadataCommand, SetSecurityCommand
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.core.xmp import read_xmp_field
    from pdfeditor.model.metadata import Metadata

    session = DocumentSession.open(fixture_pdf("report"))
    doc = session.document
    before_xmp = doc.xmp()
    session.execute(SetMetadataCommand(Metadata(title="New title", author="Bo")))
    assert read_xmp_field(doc.xmp(), "dc", "title") == "New title"
    assert doc.metadata().title == "New title"
    session.undo()
    assert doc.metadata().title == "report" and doc.xmp() == before_xmp
    session.execute(SetSecurityCommand(SecuritySettings(owner_password="x")))
    assert doc.pending_security() is not None and session.is_dirty
    session.undo()
    assert doc.pending_security() is None
    session.undo_stack.set_clean()
    session.close()
