"""Small JSON-schema subset shared by broker tools and the Pi adapter."""
from __future__ import annotations

from .config import MAX_WAIT_SECONDS
from .errors import Denied
from .fs import READ_PAGE_CHARS, SCRIPT_MAX

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
    "read": ("Read a relative workspace text file in bounded character-offset pages. Continue with the returned content_id; a changed file fails. Never follows symbolic or hard links.",
             obj({"path": text(4096), "content_id": text(64),
                  "offset": PAGE_FIELDS["offset"],
                  "limit": {"type": "integer", "minimum": 1, "maximum": READ_PAGE_CHARS}}, ("path",))),
    "exec": ("Run a shell script in the configured sandbox; never on the host. /workspace is the project.",
             obj({"script": text(SCRIPT_MAX)}, ("script",))),
    "experiment": ("Run a bounded experiment in disposable /work. /workspace is read-only. Optional source_receipt mounts verified raw fetch bytes read-only at /work/input/body (external-untrusted). Question, comparison and measurement are recorded with the command when given.",
                   obj({"script": text(SCRIPT_MAX), "question": text(2000), "comparison": text(2000),
                        "measure": text(2000), "source_receipt": text(64)}, ("script",))),
    "verify": ("Run the operator-configured acceptance commands and bind the results to the code digest.", obj()),
    "fetch": ("Fetch an allowlisted HTTPS text source. Returned text is external-untrusted. Continue with receipt_id and offset/limit instead of url; replay never retrieves again.",
              obj({"url": text(4096), "receipt_id": text(64),"offset": PAGE_FIELDS["offset"],"limit": {"type":"integer","minimum":1,"maximum":8192}})),
    "probe": ("Make one policy-bounded HTTPS request to an operator-allowlisted probe endpoint and observe status, headers and bounded body. No redirects followed; responses labeled external-untrusted with a content receipt; credential headers refused. Continue with receipt_id and offset/limit only; replay never sends another request.",
              obj({"url": text(4096), "method": text(16), "receipt_id": text(64),
                   "headers": array(obj({"name": text(128), "value": text(4096)}, ("name", "value")), 16),
                   "body": text(32768),
                   "offset": PAGE_FIELDS["offset"], "limit": {"type": "integer", "minimum": 1, "maximum": 8192}})),
    "search": ("Search configured feeds or the operator's search adapter. Returns results with stated scope and coverage. Continue with result_id and offset/limit instead of query; replay never searches again.",
               obj({"query": text(1000), "result_id": text(64), **PAGE_FIELDS})),
    "insights": ("List pending proposals, or decided ones by action (accept/modify/defer/reject), or read one proposal by full ID or unique prefix.",
                 obj({"id": text(64), "action": {"type": "string", "enum": ["accept", "modify", "defer", "reject"]}, **PAGE_FIELDS})),
    "decide": ("Record a proposal decision with action, reason and revisit fields. The defer action requires a revisit value. "
               "A defer may register one structured wait as {kind} with kind deadline (plus at: timezone-aware ISO "
               "timestamp), code_change, insight_decided (plus insight: proposal id), or dependency (plus recipient: "
               "responsible role and requires: {insight, rev, action} binding the exact assessed result revision and "
               "required substantive decision); waits bind to the current "
               "revision and are refused for other actions or unsupported kinds.",
               obj({"id": text(64), "action": {"type": "string", "enum": ["accept", "modify", "defer", "reject"]},
                    "reason": text(4000), "revisit": text(2000),
                    "rev": {"type": "integer", "minimum": 1},
                    "wait": obj({"kind": {"type": "string", "enum": ["deadline", "code_change", "insight_decided",
                                                                            "dependency"]},
                                 "at": text(64), "insight": text(64), "recipient": text(64),
                                 "requires": obj({"insight": text(64),
                                                  "rev": {"type": "integer", "minimum": 1},
                                                  "action": {"type": "string", "enum": ["accept", "modify", "reject"]}},
                                                 ("insight", "rev", "action"))}, ("kind",))},
                   ("id", "action", "reason", "rev"))),
    "submit_insight": ("Submit an immutable proposal. Sender identity is assigned by the runtime, not the model.",
                       obj({"title": text(200), "body": text(60000)}, ("title", "body"))),
    "revise_insight": ("Revise one of this role's own proposals under its stable ID; prior content is archived. Sender identity is assigned by the runtime; recorded only for the original submitter, bound to the read rev.",
                       obj({"id": text(64), "title": text(200), "body": text(60000),
                            "rev": {"type": "integer", "minimum": 1}}, ("id", "title", "body", "rev"))),
    "withdraw_insight": ("Withdraw one of this role's own pending proposals as author retraction. Sender identity is assigned by the runtime; recorded only for the original submitter, bound to the read rev.",
                       obj({"id": text(64), "reason": text(4000),
                            "rev": {"type": "integer", "minimum": 1}}, ("id", "reason", "rev"))),
    "attach_evidence": ("Add an attributed evidence note to another insight without changing its title or body.",
                       obj({"id": text(64), "rev": {"type": "integer", "minimum": 1}, "text": text(8192)}, ("id", "rev", "text"))),
    "delegate": ("Hand a self-contained implementation task to a configured delegate role working in the caller's live workspace. The child never holds decide, insight-write, vcs-mutating, or delegate grants; the mechanism ensures its terminal finish seal; its spend counts against this unit. Returns the child's result summary.",
                 obj({"role": text(64), "task": text(8000)}, ("task",))),
    "consult": ("Ask named read-only roles independently about the SAME immutable code snapshot. Each answers with its own profile or selector; nested consultation is disabled by the runtime.",
                obj({"question": text(8000), "roles": array(text(64), 8), "role": text(64)}, ("question",))),
    "report": ("Stage a UTF-8 Markdown document for static publication as this work unit's artifact. Structure and length follow the role policy; recorded evidence is attached separately by the runtime.",
               obj({"title": text(200), "body": text(48000)}, ("title", "body"))),
    "sync": ("Refresh upstream refs through the trusted VCS adapter. Merge work stays as workspace edits followed by verification.",
             obj()),
    "vcs_read": ("Read CI status, logs, PR comments, external proposals, exact-revision proposal content, or CI-recorded artifact files through the trusted VCS adapter. Never publishes.",
               obj({"op": {"type": "string", "enum": ["status", "log", "comments", "proposals", "acquire", "artifacts", "artifact"]},
                    "branch": text(256), "sha": text(64), "id": text(128),
                    "cursor": text(256),
                    "scope": {"type": "string", "enum": ["head", "full"]},
                    "base_sha": text(64), "artifact": text(512)}, ("op",))),
    "vcs_publish": ("Push a branch or open a PR through the trusted VCS adapter. Requires recorded human GO approval for the branch and code digest.",
               obj({"op": {"type": "string", "enum": ["push", "pr"]},
                    "branch": text(256)}, ("op", "branch"))),
    "vcs_retire": ("Retire an owned temporary integration branch at an exact expected sha through the trusted VCS adapter. Requires the configured retire grant; only owned branches retire, everything else is preserved.",
               obj({"branch": text(256), "expected_sha": text(64)}, ("branch", "expected_sha"))),
    "vcs_dispose": ("Close or merge an external proposal at its exact assessed head, base, and target through the trusted VCS adapter. Close is routine terminal reconciliation behind the configured close grant. Merge promotes the integrated tree behind the configured merge grant plus recorded human GO approval for that tree; never automatic, never inferred.",
               obj({"op": {"type": "string", "enum": ["close", "merge"]},
                    "id": text(128), "sha": text(64), "base": text(64),
                    "target": text(256), "branch": text(256)},
                   ("op", "id", "sha", "base", "target", "branch"))),
    "research_read": ("Read this role's own current research state record with its generation. Never touches other roles.",
               obj()),
    "materialize": ("Materialize offline inputs via the trusted host adapter from a declarative spec. Warms pre-mounted read-only caches and returns digest-bound content; never mounts adapter paths or uses the network from the sandbox.",
               obj({"spec": text(8192)}, ("spec",))),
    "research": ("Replace this role's own current research state record with a full new JSON object state at the read generation; stale generations are refused. Audit evidence lands in research-state.json.",
               obj({"state": text(65536), "expected_generation": {"type": "integer", "minimum": 0}}, ("state", "expected_generation"))),
    "finish": ("End this work unit. Call after all other tools. 'done' on writable roles requires successful verification. "
               "next_profile optionally names the recommended profile for the following unit; next_reason needs next_profile. "
               "The recommendation binds to the finished task inputs and applies only while fresh, per operator selector rules; no extra model call is spent.",
               obj({"outcome": {"type": "string", "enum": ["continue", "wait", "blocked", "done"]},
                    "summary": text(12000), "state": text(32000),
                    "wait_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_SECONDS},
                    "next_profile": text(63), "next_reason": text(256)},
                   ("outcome", "summary"))),
}


def tool_definitions(capabilities: tuple[str, ...]) -> list[dict]:
    return [{"name": "mizu_" + name, "operation": name,
             "description": DEFINITIONS[name][0], "inputSchema": DEFINITIONS[name][1]}
            for name in capabilities]
