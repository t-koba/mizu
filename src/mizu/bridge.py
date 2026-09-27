"""Per-run, authenticated Unix-socket bridge. Tokens never enter model context."""
from __future__ import annotations

import contextlib
import hmac
import json
import secrets
import socketserver
import tempfile
import threading
from pathlib import Path
from typing import Callable

from . import platform as _platform
from .errors import MizuError
from .fs import MAX_FRAME, canonical, write_json


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16


class Bridge:
    def __init__(self, handle: Callable[[str, dict], dict], tools: list[dict], *, timeout: int):
        self.handle, self.tools, self.timeout = handle, tools, timeout
        self.token = secrets.token_hex(32)
        self.temporary = None
        self.server = None
        self.thread = None

    def __enter__(self) -> "Bridge":
        self.temporary = tempfile.TemporaryDirectory(prefix="mizu-")
        root = Path(self.temporary.name)
        _platform.secure_chmod(root, 0o700)
        self.socket = root / "bridge.sock"
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

        self.server = Server(str(self.socket), Handler)
        with contextlib.suppress(OSError, AttributeError, NotImplementedError):
            _platform.secure_chmod(self.socket, 0o600)
        write_json(self.config_file, {"protocol": 1, "socket": str(self.socket),
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
