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
import socket
import time
from pathlib import Path

from .bridge import LOOPBACK_HOSTS, connect as _bridge_connect
from .errors import Denied
from .fs import MAX_FRAME, canonical, read_json
from .protocol import MCP_VERSIONS
assert MCP_VERSIONS is not None  # re-exported: single-sourced in protocol.py, framed in mcp_loop.py


def load_bridge() -> dict:
    """Read the operator-owned bridge config referenced by environment."""
    path = os.environ.get("MIZU_BRIDGE_CONFIG")
    if not path:
        raise Denied("MIZU_BRIDGE_CONFIG is required")
    config = read_json(Path(path), None)
    if not isinstance(config, dict):
        raise Denied("Invalid bridge configuration")
    transport = config.get("transport")
    if transport not in ("unix", "tcp"):
        raise Denied("Invalid bridge configuration")
    if not isinstance(config.get("token"), str) or not config["token"] or not isinstance(config.get("tools"), list):
        raise Denied("Invalid bridge configuration")
    if transport == "unix":
        if not config.get("socket"):
            raise Denied("Invalid bridge configuration")
    elif config.get("host") not in LOOPBACK_HOSTS or (type(config.get("port")) is not int or not 1 <= config["port"] <= 65535):
        raise Denied("Invalid bridge configuration")
    return config


def forward(config: dict, operation: str, arguments: dict) -> dict:
    """Forward one operation to the parent runtime over the private channel."""
    if not isinstance(arguments, dict):
        raise Denied("Tool arguments must be an object")
    try:
        timeout_ms = int(config.get("timeout_ms", 60000))
    except (TypeError, ValueError) as exc:
        raise Denied("Invalid bridge configuration") from exc
    if timeout_ms <= 0:
        raise Denied("Invalid bridge configuration")
    # Absolute deadline: per-recv timeouts are idle-only and a trickling peer
    # would defer them indefinitely, so bound the whole call by wall-clock time.
    deadline = time.monotonic() + timeout_ms / 1000.0
    wire = canonical({"token": config["token"], "operation": operation, "arguments": arguments})
    if len(wire) > MAX_FRAME:
        raise Denied("Request exceeds byte limit")
    client = None
    try:
        client = _bridge_connect(config)
        client.sendall(wire)
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Denied("Bridge deadline exceeded")
            client.settimeout(remaining)
            try:
                chunk = client.recv(65536)
            except (socket.timeout, TimeoutError) as exc:
                raise Denied("Bridge deadline exceeded") from exc
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_FRAME:
                raise Denied("Response exceeds byte limit")
            if b"\n" in data:
                break
        end = data.index(b"\n")
        try:
            response = json.loads(data[:end].decode("utf-8"), parse_constant=lambda v: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
        except (ValueError, UnicodeError):
            raise Denied("Bridge response was not valid JSON")
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
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
    from .mcp_loop import serve_stdio
    config = bridge if bridge is not None else load_bridge()
    tools = {t["name"]: t for t in config["tools"] if isinstance(t, dict) and "name" in t}

    def list_tools() -> list[dict]:
        return [{"name": n, "description": t.get("description", ""),
                 "inputSchema": t.get("inputSchema", {}), "outputSchema": {"type": "object", "additionalProperties": True}}
                for n, t in tools.items()]

    def call_tool(name, arguments: dict) -> dict:
        if name not in tools:
            raise Denied("Unknown tool")
        operation = name[5:] if name.startswith("mizu_") else name
        return forward(config, operation, arguments)

    serve_stdio(input_stream=input_stream, output_stream=output_stream,
                server_name="mizu-bridge", list_tools=list_tools, call_tool=call_tool)


if __name__ == "__main__":
    serve()
