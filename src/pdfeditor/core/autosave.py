"""Crash recovery: periodic copies of unsaved documents, offered again after a crash."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import SaveOptions

log = logging.getLogger(__name__)

# Recovery copies favor speed over size, like undo snapshots.
RECOVERY_OPTIONS = SaveOptions(garbage=0, deflate=False)


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    uid: str
    pdf: Path
    meta: Path
    original_path: Path | None
    display_name: str
    saved_at: float
    pid: int


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RecoveryStore:
    """``<root>/<uid>.pdf`` plus ``<uid>.json`` per unsaved document."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _paths(self, uid: str) -> tuple[Path, Path]:
        return self.root / f"{uid}.pdf", self.root / f"{uid}.json"

    def save(self, session: DocumentSession) -> Path:
        """Write a recovery copy of ``session`` (atomic: temp file then rename)."""
        pdf, meta = self._paths(session.uid)
        with session.lock:
            data = session.document.to_bytes(RECOVERY_OPTIONS)
        tmp = pdf.with_suffix(".pdf.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, pdf)
        target = session.save_target()
        info = {
            "original_path": str(target) if target else None,
            "display_name": session.display_name,
            "saved_at": time.time(),
            "pid": os.getpid(),
        }
        meta.write_text(json.dumps(info, indent=2), encoding="utf-8")
        return pdf

    def discard(self, uid: str) -> None:
        for path in self._paths(uid):
            with contextlib.suppress(OSError):
                path.unlink()

    def entries(self, include_running: bool = False) -> list[RecoveryEntry]:
        """Recovery copies left by instances that are no longer running, newest first."""
        out: list[RecoveryEntry] = []
        for meta in self.root.glob("*.json"):
            uid = meta.stem
            pdf = self.root / f"{uid}.pdf"
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not pdf.exists():
                self.discard(uid)
                continue
            pid = int(info.get("pid", 0))
            if not include_running and _pid_alive(pid):
                continue  # belongs to a live instance (maybe this one)
            original = info.get("original_path")
            out.append(
                RecoveryEntry(
                    uid=uid,
                    pdf=pdf,
                    meta=meta,
                    original_path=Path(original) if original else None,
                    display_name=str(info.get("display_name") or pdf.name),
                    saved_at=float(info.get("saved_at", 0)),
                    pid=pid,
                )
            )
        return sorted(out, key=lambda e: e.saved_at, reverse=True)


class Autosaver:
    """Keeps recovery copies in sync with the dirty state of open sessions.

    Call :meth:`tick` periodically (a UI timer); it writes a copy for each session changed since
    its last copy and deletes copies of sessions that are now clean.
    """

    def __init__(self, store: RecoveryStore) -> None:
        self.store = store
        self._written: dict[str, int] = {}  # uid -> undo-stack version at last copy

    def tick(self, sessions: list[DocumentSession]) -> int:
        written = 0
        for session in sessions:
            if session.closed:
                continue
            version = session.undo_stack.version
            if not session.is_dirty:
                if session.uid in self._written:
                    self.forget(session)
            elif self._written.get(session.uid) != version:
                try:
                    self.store.save(session)
                    self._written[session.uid] = version
                    written += 1
                except Exception:
                    log.exception("autosave failed for %s", session.display_name)
        return written

    def forget(self, session: DocumentSession) -> None:
        """Drop the recovery copy (after a save, or when the user discards changes)."""
        self._written.pop(session.uid, None)
        self.store.discard(session.uid)
