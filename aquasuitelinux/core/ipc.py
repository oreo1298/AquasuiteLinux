"""The service's UNIX socket: newline-delimited JSON requests and replies.

Request: ``{"id": 1, "method": "snapshot", "params": {}}``
Reply:   ``{"id": 1, "result": ...}`` or ``{"id": 1, "error": {"type": ..., "message": ...}}``

Anyone on the machine may read (snapshot, history, config). Changing anything requires
root, the service's own user, or membership of one of ``settings.allowed_groups``
(wheel, sudo, admin, aquasuite by default). The caller is identified with SO_PEERCRED.
"""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import socket
import socketserver
import struct
import threading
from pathlib import Path

from .api import READ_METHODS, WRITE_METHODS
from .errors import AquaError, ServiceError

log = logging.getLogger(__name__)

DEFAULT_SOCKET = "/run/aquasuitelinux/aquasuited.sock"
MAX_LINE = 16 * 1024 * 1024


def socket_path() -> Path:
    return Path(os.environ.get("AQUASUITELINUX_SOCKET") or DEFAULT_SOCKET)


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", data)


def user_groups(uid: int, gid: int) -> set[str]:
    names: set[str] = set()
    try:
        user = pwd.getpwuid(uid).pw_name
        for g in os.getgrouplist(user, gid):
            try:
                names.add(grp.getgrgid(g).gr_name)
            except KeyError:
                pass
    except KeyError:
        pass
    return names


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: Server = self.server  # type: ignore[assignment]
        try:
            _pid, uid, gid = peer_credentials(self.request)
        except OSError:
            uid, gid = -1, -1
        may_write = server.may_write(uid, gid)
        while True:
            line = self.rfile.readline(MAX_LINE)
            if not line:
                return
            try:
                req = json.loads(line)
                rid = req.get("id")
                method = req.get("method", "")
                params = req.get("params") or {}
            except (ValueError, AttributeError):
                self._send({"id": None, "error": {"type": "ProtocolError", "message": "invalid request"}})
                continue
            if method not in READ_METHODS | WRITE_METHODS:
                self._send({"id": rid, "error": {"type": "ProtocolError", "message": f"unknown method {method}"}})
                continue
            if method in WRITE_METHODS and not may_write:
                groups = ", ".join(server.allowed_groups())
                self._send({"id": rid, "error": {"type": "PermissionError", "message":
                            f"Changing settings needs root or membership in one of: {groups}"}})
                continue
            try:
                result = getattr(server.api, method)(**params)
                self._send({"id": rid, "result": result})
            except AquaError as exc:
                self._send({"id": rid, "error": {"type": type(exc).__name__, "message": str(exc)}})
            except (TypeError, ValueError, KeyError) as exc:
                self._send({"id": rid, "error": {"type": type(exc).__name__, "message": str(exc)}})
            except Exception as exc:  # noqa: BLE001 - report instead of dropping the connection
                log.exception("request %s failed", method)
                self._send({"id": rid, "error": {"type": type(exc).__name__, "message": str(exc)}})

    def _send(self, obj: dict) -> None:
        try:
            self.wfile.write(json.dumps(obj, default=str).encode() + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, api, path: Path, groups_fn=None, socket_mode: int = 0o666):
        self.api = api
        self.path = Path(path)
        self._groups_fn = groups_fn or (lambda: ["wheel", "sudo", "admin", "aquasuite"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()
        super().__init__(str(self.path), _Handler)
        os.chmod(self.path, socket_mode)
        self._thread: threading.Thread | None = None

    def allowed_groups(self) -> list[str]:
        return list(self._groups_fn())

    def may_write(self, uid: int, gid: int) -> bool:
        if uid in (0, os.getuid()):
            return True
        if uid < 0:
            return False
        return bool(user_groups(uid, gid) & set(self.allowed_groups()))

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="ipc", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        try:
            self.path.unlink()
        except OSError:
            pass


class Client:
    """A persistent connection to the service (thread-safe, reconnects once on failure)."""

    def __init__(self, path: Path | None = None, timeout: float = 8.0):
        self.path = Path(path) if path else socket_path()
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None
        self._id = 0
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            self._connect()
            return True
        except ServiceError:
            return False

    def _connect(self) -> None:
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(str(self.path))
        except OSError as exc:
            s.close()
            raise ServiceError(f"The background service is not running ({self.path}: {exc.strerror or exc})") from exc
        self._sock = s
        self._file = s.makefile("rb")

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._file.close()
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._file = None

    def call(self, method: str, **params):
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._connect()
                    self._id += 1
                    msg = json.dumps({"id": self._id, "method": method, "params": params}).encode() + b"\n"
                    self._sock.sendall(msg)
                    line = self._file.readline(MAX_LINE)
                    if not line:
                        raise ConnectionResetError("service closed the connection")
                    reply = json.loads(line)
                    break
                except (OSError, ValueError) as exc:
                    self._close()
                    if attempt == 2:
                        raise ServiceError(f"Lost the connection to the background service: {exc}") from exc
            if "error" in reply:
                err = reply["error"]
                if err.get("type") == "PermissionError":
                    raise PermissionError(err.get("message"))
                raise ServiceError(err.get("message", "service error"))
            return reply.get("result")
