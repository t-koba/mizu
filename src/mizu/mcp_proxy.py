"""Shared stdio MCP proxy for non-Pi engines (codex/claude).

This process has no authority of its own: it exposes exactly the capability
set the parent runtime placed in the bridge config file and forwards each
`tools/call` over the private bridge channel to `Context.handle`, which
enforces capabilities, bounds and budget. Tokens never enter model context;
they stay in the operator-owned bridge file and the local connection.

Wire protocol mirrors `adapters/pi/bridge-client.mjs`: one LF-framed JSON
request `{token, operation, arguments}`, one LF-framed JSON response
`{ok, result, error}`. The channel is a Unix socket where the platform
serves one, otherwise loopback TCP with the same token (see mizu.bridge);
TCP endpoints outside loopback are refused. MCP framing mirrors `editor.py`:
newline-delimited JSON-RPC on stdio, stdout exclusively protocol output.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .bridge import LOOPBACK_HOSTS, connect as _bridge_connect
from .errors import Denied, MizuError
from .fs import MAX_FRAME, canonical, read_json
from .protocol import MCP_VERSIONS


def load_bridge() -> dict:
    """Read the operator-owned bridge config referenced by environment."""
    path = os.environ.get("MIZU_BRIDGE_CONFIG")
    if not path:
        raise Denied("MIZU_BRIDGE_CONFIG is required")
    config = read_json(Path(path), None)
    if not isinstance(config, dict) or config.get("protocol") != 1:
        raise Denied("Unsupported Mizu bridge version")
    transport = config.get("transport", "unix")
    if transport not in ("unix", "tcp"):
        raise Denied("Invalid bridge configuration")
    if not config.get("token") or not isinstance(config.get("tools"), list):
        raise Denied("Invalid bridge configuration")
    if transport == "unix":
        if not config.get("socket"):
            raise Denied("Invalid bridge configuration")
    elif config.get("host") not in LOOPBACK_HOSTS or not isinstance(config.get("port"), int):
        raise Denied("Invalid bridge configuration")
    return config


def forward(config: dict, operation: str, arguments: dict) -> dict:
    """Forward one operation to the parent runtime over the private channel."""
    if not isinstance(arguments, dict):
        raise Denied("Tool arguments must be an object")
    wire = canonical({"token": config["token"], "operation": operation, "arguments": arguments})
    if len(wire) > MAX_FRAME:
        raise Denied("Request exceeds byte limit")
    client = None
    try:
        client = _bridge_connect(config)
        client.sendall(wire)
        data = bytearray()
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_FRAME:
                raise Denied("Response exceeds byte limit")
            if b"\n" in data:
                break
        end = data.index(b"\n")
        try:
            response = json.loads(data[:end].decode("utf-8"))
        except (ValueError, UnicodeError):
            raise Denied("Bridge response was not valid JSON")
        if not isinstance(response, dict) or "ok" not in response:
            raise Denied("Bridge response was not a valid envelope")
        if not response["ok"]:
            raise Denied(str(response.get("error") or "Tool refused"))
        if not isinstance(response.get("result"), dict):
            raise Denied("Bridge result was not an object")
        return response["result"]
    except (OSError, ValueError) as exc:
        if isinstance(exc, Denied):
            raise
        raise Denied(f"Bridge call failed: {exc}") from exc
    finally:
        if client is not None:
            client.close()


def serve(*, input_stream=None, output_stream=None, bridge=None) -> None:
    """Serve MCP over stdio until EOF. `bridge` injects config for tests."""
    config = bridge if bridge is not None else load_bridge()
    tools = {t["name"]: t for t in config["tools"] if isinstance(t, dict) and "name" in t}
    source = input_stream or sys.stdin.buffer
    sink = output_stream or sys.stdout.buffer
    initialized = False
    from . import __version__
    while True:
        raw = source.readline(MAX_FRAME + 1)
        if not raw:
            return
        if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            raise Denied("Oversized or unterminated MCP message")
        request_id = None
        notification = False
        try:
            try:
                request = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError):
                sink.write(canonical({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "Parse error"}}))
                sink.flush()
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or \
                    not isinstance(request.get("method"), str):
                raise ValueError("Invalid JSON-RPC request")
            request_id = request.get("id")
            if request_id is not None and type(request_id) not in (str, int):
                raise ValueError("Invalid request ID")
            notification = "id" not in request
            method, params = request["method"], request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "initialize":
                initialized = True
                offered = params.get("protocolVersion")
                version = offered if offered in MCP_VERSIONS else MCP_VERSIONS[-1]
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "mizu-bridge", "version": __version__}}
            elif method.startswith("notifications/"):
                continue
            elif method == "ping":
                result = {}
            elif not initialized:
                raise Denied("Initialize the MCP connection first")
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": t.get("description", ""),
                                     "inputSchema": t.get("inputSchema", {})}
                                    for n, t in tools.items()]}
            elif method == "tools/call":
                try:
                    name, arguments = params.get("name"), params.get("arguments", {})
                    if name not in tools:
                        raise Denied("Unknown tool")
                    operation = name[5:] if name.startswith("mizu_") else name
                    value = forward(config, operation, arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                              "isError": False}
                except (MizuError, OSError, ValueError, TypeError) as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                if not notification:
                    sink.write(canonical({"jsonrpc": "2.0", "id": request_id,
                                          "error": {"code": -32601, "message": "Method not found"}}))
                    sink.flush()
                continue
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, UnicodeError, MizuError, TypeError) as exc:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": str(exc)}}
        if not notification:
            sink.write(canonical(response))
            sink.flush()


if __name__ == "__main__":
    serve()
