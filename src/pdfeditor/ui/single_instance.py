"""Running several copies of the app side by side.

By default every launch opens its own window. With the "open files in the running window"
preference, a launch instead forwards its files to the window that's already running
(:func:`send_to_running_instance` / :class:`InstanceServer`). :class:`InstanceSlot` numbers the
running copies so each new window opens a little offset from the others.
"""

from __future__ import annotations

import getpass
import logging
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QProcess, QProcessEnvironment, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from pdfeditor.bundle import app_command, is_frozen
from pdfeditor.core.paths import data_dir

log = logging.getLogger(__name__)

NEW_INSTANCE_FLAG = "--new-instance"
# More copies than this share the last slot (and its window offset).
MAX_SLOTS = 32
# PyInstaller (6.10+) treats the one-file exe started by itself as a helper of the same run:
# it reuses the parent's unpacked _MEI folder, which the parent deletes when it exits. This
# makes the bootloader unpack afresh, so the new window is a fully independent copy.
RESET_ENVIRONMENT = "PYINSTALLER_RESET_ENVIRONMENT"


def server_name(app_id: str = "pdfeditor") -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"{app_id}-{user}"


def send_to_running_instance(
    paths: list[str], name: str | None = None, timeout_ms: int = 500
) -> bool:
    """Return True if another instance received ``paths`` (this process should then exit)."""
    socket = QLocalSocket()
    socket.connectToServer(name or server_name())
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write("\n".join(paths).encode("utf-8") + b"\n\x00")
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    # The server closes the connection once it has read everything; closing first from this side
    # can drop unread data on Windows named pipes.
    if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
        socket.waitForDisconnected(max(timeout_ms, 2000))
    return True


class InstanceServer(QObject):
    files_received = Signal(list)  # list[str]; may be empty = just "activate"

    def __init__(self, name: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name or server_name()
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._buffers: dict[QLocalSocket, bytes] = {}

    def listen(self) -> bool:
        if self._server.listen(self._name):
            return True
        # A crashed instance can leave a stale socket behind.
        QLocalServer.removeServer(self._name)
        ok = self._server.listen(self._name)
        if not ok:
            log.warning("single-instance server unavailable: %s", self._server.errorString())
        return ok

    def close(self) -> None:
        self._server.close()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._buffers[socket] = b""
            socket.readyRead.connect(lambda s=socket: self._on_ready(s))
            socket.disconnected.connect(lambda s=socket: self._finish(s))

    def _on_ready(self, socket: QLocalSocket) -> None:
        self._buffers[socket] = self._buffers.get(socket, b"") + bytes(socket.readAll().data())
        if self._buffers[socket].endswith(b"\x00"):
            self._finish(socket)

    def _finish(self, socket: QLocalSocket) -> None:
        if socket not in self._buffers:
            return
        # The sender may disconnect before readyRead is delivered: drain what's left first.
        data = self._buffers.pop(socket) + bytes(socket.readAll().data())
        text = data.rstrip(b"\x00").decode("utf-8", errors="replace")
        socket.disconnectFromServer()
        socket.deleteLater()
        self.files_received.emit([line for line in text.splitlines() if line.strip()])


def new_window_command() -> tuple[str, list[str], dict[str, str]]:
    """Program, arguments and extra environment that start another, independent copy. It
    never forwards to this one, whatever the preference says."""
    program, *args = app_command()
    env = {RESET_ENVIRONMENT: "1"} if is_frozen() else {}
    return program, [*args, NEW_INSTANCE_FLAG], env


def start_new_window() -> bool:
    """Start another copy of the app in its own window; False if it couldn't be started."""
    program, args, extra = new_window_command()
    env = QProcessEnvironment.systemEnvironment()
    for name, value in extra.items():
        env.insert(name, value)
    process = QProcess()
    process.setProgram(program)
    process.setArguments(args)
    process.setProcessEnvironment(env)
    ok = bool(process.startDetached())
    if not ok:
        log.warning("couldn't start a new window: %s %s", program, args)
    return ok


class InstanceSlot:
    """A number unique among the running copies: 0 for the first, then the lowest free one.

    Each slot is a lock file, so a copy that crashes frees its slot (Qt treats a lock whose
    process is gone as stale). The window uses it to open offset from the others.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.index = 0
        self._lock: QLockFile | None = None
        try:
            root = root or data_dir() / "instances"
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # only the window offset depends on it: never block startup
            log.warning("no instance slot (%s); opening at the saved position", exc)
            return
        self.index = MAX_SLOTS - 1
        for index in range(MAX_SLOTS):
            lock = QLockFile(str(root / f"{index}.lock"))
            lock.setStaleLockTime(0)  # stale only when its process is gone, never by age
            if lock.tryLock(0):
                self.index, self._lock = index, lock
                return
        log.warning("all %d instance slots are taken", MAX_SLOTS)

    def release(self) -> None:
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
