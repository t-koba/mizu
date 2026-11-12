"""Per-response byte bound for model-facing tool results, with paging.

Schema: a result whose canonical JSON exceeds ``[limits] tool_response_bytes``
is replaced by ``{"truncated", "operation", "cursor", "total_bytes", "offset",
"next_offset", "page", "continue"}``. ``page`` is a UTF-8 slice of the full
result's canonical JSON starting at byte ``offset``; pages concatenate to the
complete JSON. ``next_offset`` is null on the last page. ``continue`` is
``{"tool": "mizu_result_read", "cursor", "offset"}``: the one continuation tool
``result_read`` takes ``{cursor, offset}`` and returns the next page in the same
shape. Every role holding a tool other than ``finish`` receives ``result_read``
at configuration load; no other tool schema carries continuation fields.
The page is served from the copy retained under the run, never by executing
the producing tool again, so a continuation has no side effect and a mutating
tool is not repeated. Bounds: every response, including its wrapper, is at
most the configured bytes (minimum 4096); the retained copy is one file per
oversized result under ``<run>/tool-results``, removed with the run directory
by run retention. Trust: local run state; the cursor is a content digest valid
only in the run and for the role that received the first page. Failure:
unknown cursor, other role, an offset past the text or inside a UTF-8
character raise ``Denied``; a result that is not canonical JSON is returned
unchanged. ``vcs_read`` ``op=artifact`` with offset/limit keeps its own bound
(``vcs.artifact_page_bytes``).
"""
from __future__ import annotations

import re

from .errors import Denied
from .fs import canonical, digest, mkdir, read_json, write_json

CURSOR = re.compile(r"[0-9a-f]{32}\Z")
#: Operations whose results are never paged: the unit seal and the pager itself.
UNPAGED = frozenset({"finish", "result_read"})
_OVERHEAD = 1024


def _self_bounded(operation: str, arguments: dict) -> bool:
    """Paged artifact reads carry their own configured bound and offset contract
    (``vcs.artifact_page_bytes`` with ``next_offset``); layering a second
    cursor on them would make their documented page size unreachable."""
    return operation == "vcs_read" and arguments.get("op") == "artifact" and "offset" in arguments


def _page(operation: str, cursor: str, text: bytes, offset: int, bound: int) -> dict:
    total = len(text)
    if offset > total or (offset < total and 0x80 <= text[offset] < 0xC0):
        raise Denied("Invalid offset for this result")
    budget = max(1, bound - _OVERHEAD)
    while True:
        chunk = text[offset:offset + budget].decode("utf-8", "ignore")
        end = offset + len(chunk.encode("utf-8"))
        response = {"truncated": end < total, "operation": operation, "cursor": cursor,
                    "total_bytes": total, "offset": offset, "next_offset": end if end < total else None,
                    "page": chunk}
        if end < total:
            response["continue"] = {"tool": "mizu_result_read", "cursor": cursor, "offset": end}
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
    cursor = digest(operation.encode() + b"\0" + context.role.name.encode() + b"\0" + text)[:32]
    path = context.run_dir / "tool-results"
    mkdir(path)
    write_json(path / f"{cursor}.json", {"operation": operation, "role": context.role.name,
                                         "text": text.decode("utf-8")})
    return _page(operation, cursor, text, 0, bound)


def read(context, arguments: dict) -> dict:
    """``result_read``: the page of a retained result at ``arguments['offset']``."""
    cursor = arguments.get("cursor")
    if not isinstance(cursor, str) or CURSOR.fullmatch(cursor) is None:
        raise Denied("Unknown result cursor")
    try:
        record = read_json(context.run_dir / "tool-results" / f"{cursor}.json", None)
    except (OSError, ValueError):
        record = None
    if (not isinstance(record, dict) or record.get("role") != context.role.name
            or not isinstance(record.get("text"), str) or not isinstance(record.get("operation"), str)):
        raise Denied("Unknown result cursor")
    return _page(record["operation"], cursor, record["text"].encode("utf-8"),
                 arguments["offset"], context.config.limits.tool_response_bytes)
