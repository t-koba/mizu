"""A small stdio MCP server for exported, immutable Editor capsules.

This process is NOT an OS sandbox. Launch the Editor itself in a container or a
separate account with only the exported bundle and outbox mounted.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from . import __version__
from .errors import Denied, MizuError
from .fs import MAX_FRAME, PREVIEW_BYTES, atomic_write, canonical, digest, mkdir, read_json, relative_parts, safe_read, write_json
from .project import Project
from .protocol import DEFINITIONS, MCP_VERSIONS, obj, text, validate

#: Exported-bundle framing: whole-manifest reads vs recent-summary reads.
BUNDLE_MANIFEST_BYTES = 16 * 1024 * 1024
BUNDLE_CHANGES_BYTES = 262144

TOOLS = {
    "get_changes": ("Read the exported bounded diff and recent snapshot summaries.", obj({})),
    "get_status": ("Read the exact exported project state and its timestamp.", obj({})),
    "list_files": ("List the files in this immutable snapshot.", obj({})),
    "read_file": ("Read one relative path from the exported snapshot.", obj({"path": text(4096)}, ("path",))),
    "search_files": ("Search literal text in the exported snapshot; bounded to 50 matches.", obj({"query": text(1000)}, ("query",))),
    # Input schema shared with the runtime submit_insight; outbox files are
    # additionally framed by PREVIEW_BYTES at ingest.
    "submit_insight": ("Propose an idea to the Worker; no editing or control authority.",
                       DEFINITIONS["submit_insight"][1]),
}


def export(project: Project, destination: Path) -> dict:
    destination = destination.resolve()
    if destination.exists():
        raise Denied("Export destination already exists")
    mkdir(destination.parent)
    stage = Path(tempfile.mkdtemp(prefix=".export-", dir=destination.parent))
    try:
        snapshot = project.snapshots.get()
        project.snapshots.materialize(snapshot, stage / "code")
        write_json(stage / "snapshot.json", snapshot)
        write_json(stage / "changes.json", project.snapshots.changes(snapshot["id"]))
        write_json(stage / "history.json", [{k: item[k] for k in ("id", "created_at", "summary", "outcome", "code_digest")}
                                           for item in project.snapshots.history(snapshot["id"])])
        atomic_write(stage / "PROJECT.md", snapshot["goal"].encode())
        atomic_write(stage / "STATE.md", snapshot["state"].encode())
        os.rename(stage, destination)
        return {"bundle": str(destination), "snapshot": snapshot["id"], "created_at": snapshot["created_at"]}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


class Capsule:
    def __init__(self, bundle: Path, outbox: Path):
        self.bundle, self.outbox = bundle.resolve(), outbox.resolve()
        if self.bundle == self.outbox or self.bundle in self.outbox.parents or self.outbox in self.bundle.parents:
            raise Denied("Outbox must be separate from the immutable bundle")
        self.snapshot = json.loads(safe_read(self.bundle, "snapshot.json", BUNDLE_MANIFEST_BYTES))
        body = {k: v for k, v in self.snapshot.items() if k != "id"}
        if digest(canonical(body)) != self.snapshot.get("id"):
            raise Denied("Bundle manifest integrity failure")
        mkdir(self.outbox)

    def read(self, name: str) -> str:
        relative_parts(name)
        entry = self.snapshot["files"].get(name)
        if entry is None:
            raise Denied("File is not part of the exported snapshot")
        data = safe_read(self.bundle / "code", name, PREVIEW_BYTES)
        if digest(data) != entry["sha256"]:
            raise Denied("Exported file integrity failure")
        return data.decode("utf-8", "replace")

    def call(self, name: str, arguments: dict) -> dict:
        if name not in TOOLS:
            raise Denied("Unknown tool")
        validate(arguments, TOOLS[name][1])
        if name == "get_changes":
            return {"changes": json.loads(safe_read(self.bundle, "changes.json", BUNDLE_CHANGES_BYTES)),
                    "history": json.loads(safe_read(self.bundle, "history.json", BUNDLE_CHANGES_BYTES))}
        if name == "get_status":
            return {k: self.snapshot[k] for k in ("id", "created_at", "code_digest", "goal", "state", "outcome", "summary", "verification")}
        if name == "list_files":
            return {"files": sorted(self.snapshot["files"])}
        if name == "read_file":
            return {"path": arguments["path"], "text": self.read(arguments["path"])}
        if name == "search_files":
            if not arguments["query"].strip():
                raise Denied("A nonempty literal query is required")
            hits, scanned, skipped = [], 0, []
            for path, entry in sorted(self.snapshot["files"].items()):
                if entry["bytes"] > PREVIEW_BYTES or scanned + entry["bytes"] > 8 * 1024 * 1024:
                    skipped.append(path)
                    continue
                scanned += entry["bytes"]
                for line, value in enumerate(self.read(path).splitlines(), 1):
                    if arguments["query"] in value:
                        hits.append({"path": path, "line": line, "text": value[:1000]})
                        if len(hits) == 50:
                            return {"matches": hits, "truncated": True, "skipped": skipped}
            return {"matches": hits, "truncated": bool(skipped), "skipped": skipped}
        if not arguments["title"].strip() or not arguments["body"].strip():
            raise Denied("Insight title and body must be nonempty")
        insight_id = uuid.uuid4().hex
        write_json(self.outbox / f"{insight_id}.json",
                   {**arguments, "base_snapshot": self.snapshot["id"]}, exclusive=True)
        return {"id": insight_id, "status": "submitted_to_outbox", "base_snapshot": self.snapshot["id"]}


def serve(bundle: Path, outbox: Path, *, input_stream=None, output_stream=None) -> None:
    """Newline-delimited JSON-RPC; stdout is exclusively protocol output."""
    capsule = Capsule(bundle, outbox)
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
                request = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError):
                sink.write(canonical({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "Parse error"}}))
                sink.flush()
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise ValueError("Invalid JSON-RPC request")
            request_id = request.get("id")
            if request_id is not None and (type(request_id) not in (str, int)):
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
                          "serverInfo": {"name": "mizu-editor", "version": __version__}}
            elif method.startswith("notifications/"):
                continue
            elif method == "ping":
                result = {}
            elif not initialized:
                raise Denied("Initialize the MCP connection first")
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": d, "inputSchema": schema}
                                    for n, (d, schema) in TOOLS.items()]}
            elif method == "tools/call":
                try:
                    value = capsule.call(params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
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
