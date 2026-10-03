"""Shared newline-delimited JSON-RPC stdio loop for MCP servers.

Mechanism owns framing and handshake; policy owns the tool set (editor
capsule vs bridge proxy supply list/call callbacks). Single implementation
so protocol drift in one server cannot silently diverge from the other.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable

from .errors import Denied, MizuError
from .fs import MAX_FRAME, canonical
from .protocol import MCP_VERSIONS


def serve_stdio(*, input_stream=None, output_stream=None, server_name: str,
                list_tools: Callable[[], list[dict]],
                call_tool: Callable[[str, dict], dict]) -> None:
    """Run the MCP stdio loop until EOF.

    Schema/bounds: LF-framed JSON-RPC 2.0, each message <= MAX_FRAME.
    Trust: local stdio only. Retry: none (parse errors reply -32700 and
    continue). Evidence: responses on stdout only. Failure: oversized or
    unterminated frames raise Denied loudly; unknown methods reply -32601.
    """
    source = input_stream or sys.stdin.buffer
    sink = output_stream or sys.stdout.buffer
    initialized = False
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
                request = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))
            except (ValueError, UnicodeError):
                sink.write(canonical({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "Parse error"}}))
                sink.flush()
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or \
                    not isinstance(request.get("method"), str):
                raise ValueError("Invalid JSON-RPC request")
            offered_id = request.get("id")
            if "id" in request and (type(offered_id) not in (str, int) or
                    (type(offered_id) is str and len(offered_id.encode("utf-8")) > 256) or
                    (type(offered_id) is int and not -(2**63) <= offered_id < 2**63)):
                raise ValueError("Invalid request ID (string <=256 UTF-8 bytes or signed 64-bit integer)")
            request_id = offered_id
            notification = "id" not in request
            if notification and not request["method"].startswith("notifications/"):
                continue
            method, params = request["method"], request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "initialize":
                initialized = True
                offered = params.get("protocolVersion")
                version = offered if offered in MCP_VERSIONS else MCP_VERSIONS[-1]
                from . import __version__ as _v
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": server_name, "version": _v}}
            elif method.startswith("notifications/"):
                continue
            elif method == "ping":
                result = {}
            elif not initialized:
                raise Denied("Initialize the MCP connection first")
            elif method == "tools/list":
                result = {"tools": list_tools()}
            elif method == "tools/call":
                try:
                    value = call_tool(params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                              "structuredContent": value, "isError": False}
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
            wire = canonical(response)
            if len(wire) > MAX_FRAME:
                wire = canonical({"jsonrpc": "2.0", "id": request_id,
                                  "error": {"code": -32603, "message": "Response exceeds frame bound; request a smaller page"}})
            sink.write(wire)
            sink.flush()
