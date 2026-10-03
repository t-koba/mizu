"""Small JSON-schema subset shared by broker tools and the Pi adapter."""
from __future__ import annotations

from .config import MAX_WAIT_SECONDS
from .errors import Denied
from .fs import SCRIPT_MAX

#: MCP protocol versions accepted by the capsule and bridge servers.
MCP_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


def obj(properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


def text(maximum: int = 32768) -> dict:
    return {"type": "string", "maxLength": maximum}


def array(items: dict, maximum: int = 30) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum}


def validate(value, schema: dict, path: str = "arguments") -> None:
    kind = schema["type"]
    correct = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int,
               "boolean": type(value) is bool}
    if not correct.get(kind, False):
        raise Denied(f"{path} must have type {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise Denied(f"Unsupported value for {path}")
    if kind == "object":
        missing = set(schema.get("required", [])) - set(value)
        extra = set(value) - set(schema.get("properties", {}))
        if missing or (extra and not schema.get("additionalProperties", False)):
            raise Denied(f"Invalid fields for {path}; missing={sorted(missing)}, extra={sorted(extra)}")
        for key, item in value.items():
            validate(item, schema["properties"][key], f"{path}.{key}")
    elif kind == "array":
        if len(value) > schema.get("maxItems", 100):
            raise Denied(f"Too many values in {path}")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")
    elif kind == "string":
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise Denied(f"Invalid UTF-8 string at {path}") from exc
        if "\x00" in value or len(value) > schema.get("maxLength", 32768):
            raise Denied(f"Invalid or oversized string at {path}")
    elif kind == "integer":
        if not schema.get("minimum", -2147483648) <= value <= schema.get("maximum", 2147483647):
            raise Denied(f"Out-of-range integer at {path}")


PAGE_FIELDS = {"offset": {"type": "integer", "minimum": 0, "maximum": 2147483647},
               "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}

DEFINITIONS = {
    "diff": ("Read the bounded difference between this published snapshot and the preceding distinct code snapshot.", obj()),
    "files": ("List visible workspace files without reading their contents; use offset/limit for subsequent pages.", obj(PAGE_FIELDS)),
    "read": ("Read a relative workspace text file. Never follows symbolic or hard links.",
             obj({"path": text(4096)}, ("path",))),
    "exec": ("Run a shell script in the configured sandbox; never on the host. /workspace is the project.",
             obj({"script": text(SCRIPT_MAX)}, ("script",))),
    "experiment": ("Run a bounded experiment in disposable /work. /workspace is read-only. Question, comparison and measurement are recorded with the command when given.",
                   obj({"script": text(SCRIPT_MAX), "question": text(2000), "comparison": text(2000),
                        "measure": text(2000)}, ("script",))),
    "verify": ("Run the operator-configured acceptance commands and bind the results to the code digest.", obj()),
    "fetch": ("Fetch an allowlisted HTTPS text source. Returned text is labeled external-untrusted with a content receipt.",
              obj({"url": text(4096),"offset": PAGE_FIELDS["offset"],"limit": {"type":"integer","minimum":1,"maximum":8192}}, ("url",))),
    "search": ("Search configured feeds or the operator's search adapter. Returns results with stated scope and coverage.",
               obj({"query": text(1000),**PAGE_FIELDS}, ("query",))),
    "insights": ("List pending proposals, or read one proposal by ID.",
                 obj({"id": text(64), **PAGE_FIELDS})),
    "decide": ("Record a proposal decision with action, reason and revisit fields. The defer action requires a revisit value.",
               obj({"id": text(64), "action": {"type": "string", "enum": ["accept", "modify", "defer", "reject"]},
                    "reason": text(4000), "revisit": text(2000)}, ("id", "action", "reason"))),
    "submit_insight": ("Submit an immutable proposal. Sender identity is assigned by the runtime, not the model.",
                       obj({"title": text(200), "body": text(60000)}, ("title", "body"))),
    "consult": ("Ask configured models independently about the SAME immutable code snapshot. Nested consultation is disabled by the runtime.",
                obj({"question": text(8000), "profiles": array(text(64), 8), "role": text(64)}, ("question",))),
    "report": ("Stage a UTF-8 Markdown document for static publication as this work unit's artifact. Structure and length follow the role policy; recorded evidence is attached separately by the runtime.",
               obj({"title": text(200), "body": text(48000)}, ("title", "body"))),
    "finish": ("End this work unit. Call after all other tools. 'done' on writable roles requires successful verification.",
               obj({"outcome": {"type": "string", "enum": ["continue", "wait", "blocked", "done"]},
                    "summary": text(12000), "state": text(32000),
                    "wait_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_SECONDS}},
                   ("outcome", "summary"))),
}


def tool_definitions(capabilities: tuple[str, ...]) -> list[dict]:
    return [{"name": "mizu_" + name, "operation": name,
             "description": DEFINITIONS[name][0], "inputSchema": DEFINITIONS[name][1]}
            for name in capabilities]
