"""One Timmy per user. A second launch (Start menu shortcut while Timmy already runs) asks the running one to open
"השיחות שלי" and exits, like the Mac app ("אם הוא כבר פתוח, ייפתח מסך ההגדרות").

QLocalServer = a named pipe on Windows (\\\\.\\pipe\\<name>), a socket file elsewhere. The name contains the user
name so two Windows users on one PC each get their own Timmy. TIMMY_INSTANCE_NAME overrides it (tests).
"""
import getpass
import hashlib
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def server_name() -> str:
    env = os.environ.get("TIMMY_INSTANCE_NAME")
    if env:
        return env
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return "timmy-" + hashlib.sha1(user.encode("utf-8")).hexdigest()[:12]


def send_to_running(message: str = "show", name: str | None = None, timeout_ms: int = 800) -> bool:
    """True when another Timmy answered (then this process should exit)."""
    s = QLocalSocket()
    s.connectToServer(name or server_name())
    if not s.waitForConnected(timeout_ms):
        return False
    s.write((message + "\n").encode("utf-8"))
    s.flush()
    s.waitForBytesWritten(timeout_ms)
    s.disconnectFromServer()
    if s.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        s.waitForDisconnected(timeout_ms)
    return True


class SingleInstance(QObject):
    """listen() -> False when another instance already serves the name. `message` fires with each line received."""
    message = Signal(str)

    def __init__(self, name: str | None = None, parent=None):
        super().__init__(parent)
        self.name = name or server_name()
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept)
        self._bufs: dict[int, bytes] = {}

    def listen(self) -> bool:
        if send_to_running("ping", self.name):
            return False
        if not self.server.listen(self.name):
            # a crashed Timmy can leave a stale socket file (not on Windows, where pipes vanish with the process)
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                return False
        return True

    def _accept(self):
        # On Windows (named pipe) the second Timmy writes its line and disconnects at once: the bytes can already be
        # waiting when the connection is accepted (no readyRead follows) or arrive together with the disconnect. So
        # read on accept, on readyRead and on disconnect (windows-latest: the "show" of a second launch was lost).
        # The slots are bound methods using sender(): no lambda keeps a socket wrapper alive past its C++ object.
        while self.server.hasPendingConnections():
            c = self.server.nextPendingConnection()
            if c is None:
                break
            self._bufs[id(c)] = b""
            c.readyRead.connect(self._on_ready)
            c.disconnected.connect(self._on_gone)
            self._drain(c)
            if c.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                self._done(c)

    def _on_ready(self):
        c = self.sender()
        if isinstance(c, QLocalSocket):
            self._drain(c)

    def _on_gone(self):
        c = self.sender()
        if isinstance(c, QLocalSocket):
            self._done(c)

    def _drain(self, c):
        buf = self._bufs.get(id(c), b"") + bytes(c.readAll())
        *lines, rest = buf.split(b"\n")
        self._bufs[id(c)] = rest
        for raw in lines:
            self._emit(raw)

    def _done(self, c):
        if id(c) not in self._bufs:
            return
        self._drain(c)
        rest = self._bufs.pop(id(c), b"")
        if rest:
            self._emit(rest)
        c.deleteLater()

    def _emit(self, raw: bytes):
        line = raw.decode("utf-8", "replace").strip()
        if line and line != "ping":
            self.message.emit(line)

    def close(self):
        self.server.close()
