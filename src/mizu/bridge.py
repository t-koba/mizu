"""Per-run, authenticated local bridge. Tokens never enter model context.

Transport schema (bridge.json, protocol 1)::

    {"protocol": 1, "transport": "unix" | "tcp",
     "socket": str | null, "host": str | null, "port": int | null,
     "token": str, "timeout_ms": int, "tools": [...]}

Unix-socket servers exist on POSIX; where they do not (Windows) the bridge
serves 127.0.0.1 on an ephemeral port instead. Both transports carry the same
LF-framed JSON envelope, the same per-run 256-bit token compared with
hmac.compare_digest, and the same bounds (MAX_FRAME each way, timeout from
run limits, request queue 16). Clients older than the ``transport`` field
treat a missing field as ``"unix"``.

Trust: the config file is operator-owned and never mounted into command
containers; the token is per-run and never enters model context. TCP binds
loopback only and dialing refuses non-loopback endpoints, so a tampered
config cannot redirect tool calls (and their file contents) off the host.
On Windows, file ACLs differ from POSIX modes (see platform.secure_chmod):
production secrecy still requires a Linux host.

Cancellation/retry: serve_forever polls every 0.1s; __exit__ shuts down,
closes and joins the worker thread (2s). Clients set a socket timeout from
timeout_ms and perform no retries; a failed call surfaces to the run record.

Failure behavior: invalid configs and unsupported transports raise Denied
loudly; unreachable servers and framing violations become {"ok": False}
responses or Denied bridge errors, never an unauthenticated success.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import secrets
import socket
import socketserver
import tempfile
import threading
from pathlib import Path
from typing import Callable

from . import platform as _platform
from .errors import Denied, MizuError
from .fs import MAX_FRAME, canonical, write_json

#: Loopback hosts a TCP bridge may bind or dial. Anything else is refused so
#: a tampered bridge file cannot redirect tool calls off the host.
LOOPBACK_HOSTS = ("127.0.0.1", "::1")


def default_transport() -> str:
    """Unix sockets where the platform serves them, else loopback TCP."""
    return "unix" if _platform.HAS_UNIX_SOCKET_SERVER else "tcp"


if _platform.HAS_UNIX_SOCKET_SERVER:
    class _UnixBridgeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):  # type: ignore[attr-defined]
        daemon_threads = True
        block_on_close = False
        request_queue_size = 16


class _TCPBridgeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16


def connect(config: dict) -> socket.socket:
    """Dial the bridge described by a bridge.json dict.

    Only loopback TCP endpoints are allowed; Unix paths are dialed with
    AF_UNIX where the platform provides it. Anything else raises Denied.
    """
    try:
        timeout_ms = int(config.get("timeout_ms", 60000))
    except (TypeError, ValueError) as exc:
        raise Denied("Invalid bridge configuration") from exc
    timeout = max(1, timeout_ms // 1000)
    transport = config.get("transport", "unix")
    if transport == "tcp":
        host, port = config.get("host"), config.get("port")
        if host not in LOOPBACK_HOSTS or not isinstance(port, int) or not 1 <= port <= 65535:
            raise Denied("Bridge TCP endpoint must be loopback with a valid port")
        return socket.create_connection((host, port), timeout=timeout)
    if transport != "unix":
        raise Denied(f"Unsupported bridge transport: {transport}")
    path = config.get("socket")
    if not isinstance(path, str) or not path:
        raise Denied("Invalid bridge configuration")
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        raise Denied("Unix-socket bridge is not supported on this platform")
    client = socket.socket(family, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(path)
    except OSError:
        client.close()
        raise
    return client


class Bridge:
    def __init__(self, handle: Callable[[str, dict], dict], tools: list[dict], *, timeout: int,
                 transport: str | None = None):
        self.handle, self.tools, self.timeout = handle, tools, timeout
        if transport is None:
            transport = default_transport()
        if transport not in ("unix", "tcp"):
            raise Denied(f"Unsupported bridge transport: {transport}")
        if transport == "unix" and not _platform.HAS_UNIX_SOCKET_SERVER:
            raise Denied("Unix-socket bridge is not supported on this platform")
        self.transport = transport
        self.token = secrets.token_hex(32)
        self.temporary = None
        self.server = None
        self.thread = None
        self.socket = None
        self.host = None
        self.port = None

    def __enter__(self) -> "Bridge":
        self.temporary = tempfile.TemporaryDirectory(prefix="mizu-")
        root = Path(self.temporary.name)
        _platform.secure_chmod(root, 0o700)
        self.config_file = root / "bridge.json"
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(outer.timeout + 10)
                try:
                    raw = self.rfile.readline(MAX_FRAME + 1)
                    if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                        raise ValueError("Invalid bridge frame")
                    request = json.loads(raw)
                    if not isinstance(request, dict) or not isinstance(request.get("token"), str):
                        raise ValueError("Unauthenticated request")
                    if not hmac.compare_digest(request["token"], outer.token):
                        raise ValueError("Unauthenticated request")
                    if set(request) != {"token", "operation", "arguments"}:
                        raise ValueError("Invalid request fields")
                    result = outer.handle(request["operation"], request["arguments"])
                    response = {"ok": True, "result": result}
                except (MizuError, ValueError, TypeError, KeyError, OSError) as exc:
                    response = {"ok": False, "error": str(exc)}
                except Exception:
                    response = {"ok": False, "error": "Internal tool failure; inspect the run record"}
                try:
                    self.wfile.write(canonical(response))
                    self.wfile.flush()
                except (BrokenPipeError, OSError):
                    pass

        if self.transport == "unix":
            self.socket = root / "bridge.sock"
            self.server = _UnixBridgeServer(str(self.socket), Handler)
            with contextlib.suppress(OSError, AttributeError, NotImplementedError):
                _platform.secure_chmod(self.socket, 0o600)
            endpoint: dict = {"transport": "unix", "socket": str(self.socket), "host": None, "port": None}
        else:
            self.server = _TCPBridgeServer(("127.0.0.1", 0), Handler)
            self.host = "127.0.0.1"
            self.port = self.server.server_address[1]
            endpoint = {"transport": "tcp", "socket": None, "host": self.host, "port": self.port}
        write_json(self.config_file, {"protocol": 1, **endpoint,
                                      "token": self.token, "timeout_ms": (self.timeout + 5) * 1000,
                                      "tools": self.tools})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                       name="mizu-bridge", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)
        if self.temporary:
            self.temporary.cleanup()
