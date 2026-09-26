"""Single-instance support: a second launch forwards its files to the running window."""

from __future__ import annotations

import getpass
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)


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
