"""Per-response byte bound for model-facing tool results, with paging.

Schema: a result whose canonical JSON exceeds ``[limits] tool_response_bytes``
is replaced by ``{"truncated", "operation", "result_cursor", "total_bytes",
"offset", "next_offset", "page", "continue"}``. ``page`` is a UTF-8 slice of
the full result's canonical JSON starting at byte ``offset``; pages
concatenate to the complete JSON. ``next_offset`` is null on the last page.
The caller continues by repeating the call with identical other arguments
plus ``result_cursor`` and ``result_offset`` (accepted by every tool except
``finish``); the page is served from the copy retained under the run, never by
executing the tool again, so a continuation has no side effect and a mutating
tool is not repeated. Bounds: every response, including its wrapper, is at
most the configured bytes (minimum 4096); the retained copy is one file per
oversized result under ``<run>/tool-results``, removed with the run. Trust:
local run state; the cursor is a content digest bound to the operation and
the original arguments. Failure: unknown cursor, changed arguments, or an
offset outside the text or inside a UTF-8 character raise ``Denied``; a
result that is not canonical JSON is returned unchanged. vcs_read op=artifact
with offset/limit keeps its own bound (``vcs.artifact_page_bytes``).
"""
from __future__ import annotations

from .errors import Denied
import re

from .fs import canonical, digest, mkdir, read_json, write_json

CURSOR = re.compile(r"[0-9a-f]{32}\Z")
CURSOR_FIELDS = ("result_cursor", "result_offset")
#: Operations whose results are never paged (the unit seal).
UNPAGED = frozenset({"finish"})
_OVERHEAD = 1024


def _self_bounded(operation: str, arguments: dict) -> bool:
    """Paged artifact reads carry their own configured bound and offset contract
    (``vcs.artifact_page_bytes`` with ``next_offset``); layering a second
    cursor on them would make their documented page size unreachable."""
    return operation == "vcs_read" and arguments.get("op") == "artifact" and "offset" in arguments


def schema_fields() -> dict:
    return {"result_cursor": {"type": "string", "minLength": 32, "maxLength": 32},
            "result_offset": {"type": "integer", "minimum": 0, "maximum": 9007199254740991}}


def _arguments_digest(arguments: dict) -> str:
    return digest(canonical({k: v for k, v in arguments.items() if k not in CURSOR_FIELDS}))


def _store(context):
    path = context.run_dir / "tool-results"
    mkdir(path)
    return path


def _page(context, operation: str, cursor: str, text: bytes, offset: int, bound: int) -> dict:
    total = len(text)
    if offset > total or (offset < total and 0x80 <= text[offset] < 0xC0):
        raise Denied("Invalid result_offset for this result")
    budget = max(1, bound - _OVERHEAD)
    while True:
        chunk = text[offset:offset + budget].decode("utf-8", "ignore")
        end = offset + len(chunk.encode("utf-8"))
        response = {"truncated": end < total, "operation": operation, "result_cursor": cursor,
                    "total_bytes": total, "offset": offset, "next_offset": end if end < total else None,
                    "page": chunk}
        if end < total:
            response["continue"] = ("Repeat this call with the same arguments plus result_cursor and "
                                    f"result_offset={end}; pages concatenate to the complete JSON result.")
        excess = len(canonical(response)) - bound
        if excess <= 0 or budget <= 1:
            return response
        budget = max(1, budget - excess)


def bounded(context, operation: str, arguments: dict, result):
    """Return ``result`` unchanged when within the bound, else its first page."""
    if operation in UNPAGED or _self_bounded(operation, arguments):
        return result
    bound = context.config.limits.tool_response_bytes
    try:
        text = canonical(result)
    except (TypeError, ValueError):
        return result
    if len(text) <= bound:
        return result
    cursor = digest(operation.encode() + b"\0" + text)[:32]
    path = _store(context) / f"{cursor}.json"
    write_json(path, {"operation": operation, "arguments": _arguments_digest(arguments),
                      "text": text.decode("utf-8")})
    return _page(context, operation, cursor, text, 0, bound)


def continuation(context, operation: str, arguments: dict):
    """Serve a page of a retained result, or None when no cursor is present."""
    if "result_cursor" not in arguments and "result_offset" not in arguments:
        return None
    cursor = arguments.get("result_cursor")
    if not isinstance(cursor, str) or CURSOR.fullmatch(cursor) is None:
        raise Denied("result_offset requires a valid result_cursor")
    path = context.run_dir / "tool-results" / f"{cursor}.json"
    try:
        record = read_json(path, None)
    except (OSError, ValueError):
        record = None
    if (not isinstance(record, dict) or record.get("operation") != operation
            or not isinstance(record.get("text"), str)):
        raise Denied("Unknown result cursor for this tool")
    if record.get("arguments") != _arguments_digest(arguments):
        raise Denied("Continuation arguments differ from the original call")
    return _page(context, operation, cursor, record["text"].encode("utf-8"),
                 arguments.get("result_offset", 0), context.config.limits.tool_response_bytes)
