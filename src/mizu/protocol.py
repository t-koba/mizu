"""Small JSON-schema subset shared by broker tools and the Pi adapter."""
from __future__ import annotations

import re

from .config import MAX_WAIT_SECONDS
from .errors import Denied
from .fs import READ_PAGE_CHARS, SCRIPT_MAX
from .work_item_seed import MAX_DECISION_TASKS
from .work_items import MAX_OPEN_CURSOR_CHARS, MAX_OPEN_PAGE_ROWS, MAX_REVISIONS, STATUSES

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
        if len(value) < schema.get("minProperties", 0):
            raise Denied(f"Too few properties for {path}")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise Denied(f"Too many properties for {path}")
        for key, item in value.items():
            validate(item, schema["properties"][key], f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise Denied(f"Too few values in {path}")
        if len(value) > schema.get("maxItems", 100):
            raise Denied(f"Too many values in {path}")
        if schema.get("uniqueItems") and any(v in value[:i] for i, v in enumerate(value)):
            raise Denied(f"Duplicate values in {path}")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")
    elif kind == "string":
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise Denied(f"Invalid UTF-8 string at {path}") from exc
        if ("\x00" in value or len(value) < schema.get("minLength", 0)
                or len(value) > schema.get("maxLength", 32768)
                or ("pattern" in schema and re.fullmatch(schema["pattern"], value) is None)):
            raise Denied(f"Invalid or oversized string at {path}")
    elif kind == "integer":
        if not schema.get("minimum", -2147483648) <= value <= schema.get("maximum", 2147483647):
            raise Denied(f"Out-of-range integer at {path}")


PAGE_FIELDS = {"offset": {"type": "integer", "minimum": 0, "maximum": 2147483647},
               "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}

EVIDENCE_READ_FIELDS = {"run": text(32), "evidence_id": text(32),
                        "content_id": text(64), "offset": PAGE_FIELDS["offset"],
                        "limit": {"type": "integer", "minimum": 1, "maximum": READ_PAGE_CHARS}}

# Shared schema for the structured waits accepted by `decide` and blocked
# work-item transitions. Store-side normalization supplies their semantics.
WAIT_SCHEMA = obj({"kind": {"type": "string", "enum": ["deadline", "code_change", "insight_decided",
                                                               "dependency"]},
                    "at": text(64), "insight": text(64), "recipient": text(64),
                    "requires": obj({"insight": text(64),
                                     "rev": {"type": "integer", "minimum": 1},
                                     "action": {"type": "string", "enum": ["accept", "modify", "reject"]}},
                                    ("insight", "rev", "action"))}, ("kind",))

#: One accepted-scope statement: the work a decision or item authorizes.
#: 1-4000 nonblank characters, like the immutable description.
SCOPE_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 4000,
                 "pattern": "[\\s\\S]*\\S[\\s\\S]*"}

# CI is deliberately work-item-only; insight deferrals retain WAIT_SCHEMA.
WORK_ITEM_WAIT_SCHEMA = obj({**WAIT_SCHEMA["properties"],
    "kind": {"type": "string", "enum": WAIT_SCHEMA["properties"]["kind"]["enum"] + ["ci"]},
    "publication_id": {"type": "string", "pattern": "^[0-9a-f]{64}$", "maxLength": 64},
    "checks": dict(array({"type": "string", "minLength": 1, "maxLength": 256}, 64),
                   minItems=1, uniqueItems=True)}, ("kind",))

DEFINITIONS = {
    "diff": ("Read the bounded difference between this published snapshot and the role's last observed snapshot when that cursor is still retained and ancestral, otherwise the preceding distinct code snapshot. The result names base_snapshot.", obj()),
    "files": ("List visible workspace files without reading their contents; use offset/limit for subsequent pages.", obj(PAGE_FIELDS)),
    "read": ("Read a relative workspace text file in bounded character-offset pages. The first read of a path in this unit may start at any offset and pins its content identity. Later pages may omit content_id to use that pin; a changed file fails. Start at offset=0 to read a new version, and pass content_id to require continuity across units. The host retains at most 1024 path identities per unit. Never follows symbolic or hard links.",
             obj({"path": text(4096), "content_id": text(64),
                  "offset": PAGE_FIELDS["offset"],
                  "limit": {"type": "integer", "minimum": 1, "maximum": READ_PAGE_CHARS}}, ("path",))),
    "command_evidence_read": ("Read one retained command record from this project's same role's run (current or prior). Full run and evidence UUIDs required; no listing. Local recorded data is untrusted. Continue with content_id; changed or unavailable records fail.",
                              obj(EVIDENCE_READ_FIELDS, ("run", "evidence_id"))),
    "experiment_evidence_read": ("Read one retained experiment wrapper from this project's same role's run (current or prior). Full run and evidence UUIDs required; no listing. Local recorded data is untrusted. Continue with content_id; changed or unavailable records fail.",
                                 obj(EVIDENCE_READ_FIELDS, ("run", "evidence_id"))),
    "exec": ("Run a shell script in the configured sandbox; never on the host. /workspace is the project. For read-only roles with check_job this returns a queued frozen-input job: use check_job result to collect it. Writable exec remains synchronous to protect workspace ownership.",
             obj({"script": text(SCRIPT_MAX)}, ("script",))),
    "experiment": ("Run a bounded experiment in disposable /work. When check_job is granted it returns a queued frozen-input job; collect it with check_job result and continue independent work or finish wait while pending. /workspace is read-only. Optional source_receipt mounts verified raw fetch bytes read-only at /work/input/body (external-untrusted). Question, comparison and measurement are recorded with the command when given.",
                   obj({"script": text(SCRIPT_MAX), "question": text(2000), "comparison": text(2000),
                        "measure": text(2000), "source_receipt": text(64)}, ("script",))),
    "check_job": ("Start an asynchronous sandbox check on a frozen source copy (action=start; omit script for acceptance commands, or give script for an experiment; fresh=true bypasses reuse). Returns immediately: continue independent work, or follow the returned wait recipe to finish and release your model slot and workspace lock; completion wakes you without sleeping or repeated polling. action=logs with id and no evidence_id lists up to 32 available command evidence IDs and ready-to-use requests; with evidence_id and offset/limit it reads recorded command output (continue with content_id); action=cancel with id requests bounded cancellation; action=list lists your jobs and compatible shared checks; action=join with an available id subscribes without repeating execution; each subscriber cancels and acknowledges independently; action=result with id reads/acknowledges completion and imports passing acceptance only if current source/environment still match. action=start takes an optional purpose (1-64 printable characters, no path separators) naming what the demand is for: a later start with the same purpose from the same role replaces that role's earlier unfinished job for that purpose, which then leaves the queue and keeps its evidence; a purpose never retires another role's job. Completion wakes the requesting role. Never assume a queued or failed job passed.",
                  obj({"action": {"type": "string", "enum": ["start", "list", "result", "cancel", "logs", "join"]}, "fresh": {"type": "boolean"}, "purpose": text(64), "id": text(64), "script": text(SCRIPT_MAX), "evidence_id": text(32), "content_id": text(64), "offset": PAGE_FIELDS["offset"], "limit": {"type": "integer", "minimum": 1, "maximum": 8192}}, ("action",))),
    "verify": ("Run the operator-configured acceptance commands and bind the results to the code digest. When check_job is granted this queues a frozen-input job and returns immediately; use check_job result to collect it, continue independent work or finish wait while pending.", obj()),
    "fetch": ("Fetch an allowlisted HTTPS text source. Returned text is external-untrusted. Continue with receipt_id and offset/limit instead of url; replay never retrieves again.",
              obj({"url": text(4096), "receipt_id": text(64),"offset": PAGE_FIELDS["offset"],"limit": {"type":"integer","minimum":1,"maximum":8192}})),
    "probe": ("Make one policy-bounded HTTPS request to an operator-allowlisted probe endpoint and observe status, headers and bounded body. No redirects followed; responses labeled external-untrusted with a content receipt; credential headers refused. Continue with receipt_id and offset/limit only; replay never sends another request.",
              obj({"url": text(4096), "method": text(16), "receipt_id": text(64),
                   "headers": array(obj({"name": text(128), "value": text(4096)}, ("name", "value")), 16),
                   "body": text(32768),
                   "offset": PAGE_FIELDS["offset"], "limit": {"type": "integer", "minimum": 1, "maximum": 8192}})),
    "search": ("Search configured feeds or the operator's search adapter. Returns results with stated scope and coverage. Continue with result_id and offset/limit instead of query; replay never searches again.",
               obj({"query": text(1000), "result_id": text(64), **PAGE_FIELDS})),
    "insights": ("List pending proposals, or decided ones by action (accept/modify/defer/reject), or read one proposal by full ID or unique prefix. On an ID read only, the original submitter may also receive a small current work_item_status for a valid accepted/modified work item, including its ID, status, revision, holder, updated time, and origin staleness; other roles and list responses do not receive it. For explicit task groups work_item_status is {items: [status, ...]} in declared order, bounded by PREVIEW_BYTES; single-item decisions retain their existing status shape. This projection does not transition, schedule, or wake work, and grants no work-item access.",
                 obj({"id": text(64), "action": {"type": "string", "enum": ["accept", "modify", "defer", "reject"]}, **PAGE_FIELDS})),
    "work_items_read": ("Read a bounded page of currently open work items, or inspect one exact seeded item by id in any status with current origin-staleness and immutable ascending revision history including seed revision 1. Open-page mode accepts cursor (max 128 characters) and limit 1-100 (default 25); by-ID mode accepts exclusive after_rev 0-256 (default 0) and limit 1-100 (default 25), and cannot use cursor. Descriptions, when present, are immutable full text in by-ID item/history reads and 1024 UTF-8-byte description_preview with description_truncated in compact open rows. Existing seeds without descriptions remain valid. Each open row is at most 8192 canonical bytes; receipt-pinned CI waits bound the serialized declared check set to 2048 UTF-8 bytes so compact rows remain within this limit. The complete response is at most 1 MiB. By-ID history returns at most 100 full records (each bounded by the 128 KiB record reader) and stops at the row or byte bound but replays and validates the complete chain, up to the 256-revision chain bound; history_total is current rev, cursor echoes after_rev, and next_cursor is the last returned rev when more history remains. Local records are untrusted. Reads are fresh live projections and do not transition items; recognized atomic-write leftovers may be recovered under the shared lock. Unsafe or malformed store data fails closed.",
                        obj({"id": {"type": "string", "minLength": 35, "maxLength": 35,
                                    "pattern": "^wi_[0-9a-f]{32}$"},
                             "cursor": text(MAX_OPEN_CURSOR_CHARS),
                             "after_rev": {"type": "integer", "minimum": 0,
                                           "maximum": MAX_REVISIONS},
                             "limit": {"type": "integer", "minimum": 1,
                                       "maximum": MAX_OPEN_PAGE_ROWS}})),
    "work_items_legacy_read": ("Audit a bounded live page of legacy accept/modify decision events, including unresolved and open/drop reconciled events and explicit no-work dispositions (resolution.outcome=none, not openable). No proposal bodies; reasons are bounded previews. Optional opaque cursor (max 128 characters), limit 1-100 (default 25); rows at most 8192 canonical bytes and response at most 1 MiB. Recorded content is untrusted. Recognized atomic-write leftovers may be recovered; unsafe committed data fails closed.",
                               obj({"cursor": text(MAX_OPEN_CURSOR_CHARS),
                                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_OPEN_PAGE_ROWS}})),
    "work_items_reconcile": ("Resolve exactly one legacy unseeded accept/modify event by immutable open/drop sidecar without rewriting decisions. Actor is fixed to the runtime role. Expected action and optional revision are checked against the source event. Nonempty reason required; open requires title (1-200 characters) and valid source origin/revision/timestamp; drop has no title or seed. Exact retries return {record, created:false} and do not count as progress; first writes return created:true. Conflicting retries and explicitly no-work events are denied. No scheduling effects.",
                             obj({"event_id": {"type": "string", "pattern": "^[0-9a-f]{32}$", "minLength": 32, "maxLength": 32},
                                  "expected_action": {"type": "string", "enum": ["accept", "modify"]},
                                  "expected_rev": {"type": "integer", "minimum": 1},
                                  "outcome": {"type": "string", "enum": ["open", "drop"]},
                                  "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
                                  "title": {"type": "string", "minLength": 1, "maxLength": 200}},
                                 ("event_id", "expected_action", "outcome", "reason"))),
    "work_items_create": ("Create self-directed or split-off work with a stable caller-chosen idempotency_key (1-128 characters) and title (1-200). Optional immutable description is 1-4000 nonblank characters; compact rows/events carry a 1024 UTF-8-byte preview and truncation flag, by-ID reads retain the full text. Omission preserves legacy seed shape. Optional immutable scope (1-4000 nonblank characters) states the accepted scope of this work: later revisions inherit it, a change of it is its own recorded event, and a closure binds it. Description and scope presence and exact text are part of retry identity. Actor is bound to the runtime role, never supplied. Optional origin has exactly one of insight_id or work_item_id plus rev; non-current referenced revisions are denied on first commit. Omitted origin is null in storage. Keys are actor-scoped: exact retries return the original immutable {record, created:false}, even after transitions; conflicting retries are denied. First commit returns created:true. No automatic retry, scheduling, decision or assignment; no model-supplied actor. Shared bounded seed/history readers and done-verification checks apply. Unsafe or malformed state fails closed; local I/O errors propagate. Retain the same key and request after cancellation or an uncertain response.",
                          obj({"idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
                               "title": {"type": "string", "minLength": 1, "maxLength": 200},
                               "description": {"type": "string", "minLength": 1, "maxLength": 4000,
                                               "pattern": "[\\s\\S]*\\S[\\s\\S]*"},
                               "scope": SCOPE_SCHEMA,
                               "origin": dict(obj({"insight_id": text(128),
                                              "work_item_id": {"type": "string", "pattern": "^wi_[0-9a-f]{32}$"},
                                              "rev": {"type": "integer", "minimum": 1}}, ("rev",)),
                                              minProperties=2, maxProperties=2)},
                              ("idempotency_key", "title"))),
    "work_items_transition": ("Append one work-item state transition using the required expected_rev CAS. Re-read the current work-item projection/revision after a stale-CAS refusal; retries are not automatic. Actor is bound to the runtime role, never supplied by the caller. An optional scope (1-4000 nonblank characters) states the accepted scope in force from this revision on; omitting it inherits the scope in force, a scope is never cleared, and a done closure may not change the scope it inherits (record a scope change in an earlier transition). Done requires snapshot_id for retained published history with passing digest-bound verification, and that snapshot carries the delivery: the ledger derives the tree the workspace presents and refuses a closure bound to another tree; dropped requires a nonempty reason; blocked may register a decide-style wait or a work-item-only CI wait {kind:ci, publication_id, checks} with 1-64 unique exact check names and a 2048-byte serialized-set bound; automatic VCS polling and a completed publication receipt are required. Terminal CI evidence wakes the holder without changing status. Terminal transitions may wake explicitly subscribed roles; this does not schedule or assign work.",
                               obj({"id": text(64), "expected_rev": {"type": "integer", "minimum": 1},
                                    "status": {"type": "string", "enum": list(STATUSES)},
                                    "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
                                    "wait": WORK_ITEM_WAIT_SCHEMA,
                                    "scope": SCOPE_SCHEMA,
                                    "snapshot_id": {"type": "string", "minLength": 64, "maxLength": 64,
                                                    "pattern": "^[0-9a-f]{64}$"}},
                                   ("id", "expected_rev", "status"))),
    "decide": ("New create-intent seeds store the exact decision reason as immutable description (1-4000 nonblank characters); replay validates that binding when present and keeps existing seeds without descriptions unchanged. Accept/modify may state the accepted scope (1-4000 nonblank characters): the work the decision authorizes. It is recorded with the decision, carried by the seeded work item, inherited by every later revision and bound by its closure; a change of it is its own recorded event, never prose in a reason. Defer and reject store no scope. Record a proposal decision with action, reason and revisit fields. The defer action requires a revisit value. "
               "A defer may register one structured wait as {kind} with kind deadline (plus at: timezone-aware ISO "
               "timestamp), code_change, insight_decided (plus insight: proposal id), or dependency (plus recipient: "
               "responsible role and requires: {insight, rev, action} binding the exact assessed result revision and "
               "required substantive decision); waits bind to the current "
               "revision and are refused for other actions or unsupported kinds. "
               "Accept/modify optionally take work_item_intent=create|none; omission uses configured trusted-source "
               "policy (global default create). None durably records no work without a seed. "
               "Optional work_items names 1-16 tasks, each with title (1-200 nonblank characters) and description "
               "(1-4000 nonblank characters), optionally wait using the same four defer kinds (not ci). "
               "Tasks require create intent (explicit create if source policy is none); "
               "an empty list or explicit none is denied. The complete event including specs, normalized wait manifest "
               "and seeds is bounded "
               "to 131072 UTF-8 bytes and committed atomically. Each task inherits the decision scope and is unheld; "
               "without wait it starts open, with wait it starts blocked carrying the normalized condition. "
               "Each has a deterministic identity bound to the whole ordered task list and exact decision revision. "
               "Raw wait specs are part of the ordered request identity. Exact replays return stored baselines, never rebase "
               "code_change waits; omissions or changes conflict. "
               "Conflicting decisions after accept/modify/reject require revise. Exact keyed replays return the original event; "
               "if only its current pointer failed, a matching retry restores it without a second event. Read current state after uncertainty.",
               obj({"id": text(64), "action": {"type": "string", "enum": ["accept", "modify", "defer", "reject"]},
                    "reason": text(4000), "revisit": text(2000),
                    "rev": {"type": "integer", "minimum": 1},
                    "wait": WAIT_SCHEMA,
                    "scope": SCOPE_SCHEMA,
                    "work_items": {"type": "array", "minItems": 1, "maxItems": MAX_DECISION_TASKS,
                                   "items": obj({"title": {**text(200), "minLength": 1},
                                                 "description": {**text(4000), "minLength": 1}, "wait": WAIT_SCHEMA},
                                                ("title", "description"))},
                    "work_item_intent": {"type": "string", "enum": ["create", "none"]}},
                   ("id", "action", "reason", "rev"))),
    "submit_insight": ("Submit an immutable proposal. Sender identity is assigned by the runtime, not the model.",
                       obj({"title": text(200), "body": text(60000)}, ("title", "body"))),
    "revise_insight": ("Revise one of this role's own proposals under its stable ID; prior content is archived. Sender identity is assigned by the runtime; recorded only for the original submitter, bound to the read rev.",
                       obj({"id": text(64), "title": text(200), "body": text(60000),
                            "rev": {"type": "integer", "minimum": 1}}, ("id", "title", "body", "rev"))),
    "withdraw_insight": ("Withdraw one of this role's own proposals as author retraction. A current accept/modify blocks withdrawal unless the role also has withdraw_decided. Sender identity is assigned by the runtime and the action is bound to the read rev.",
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
    "vcs_read": ("Read CI status, logs, PR comments, external proposals, exact-revision proposal content, or CI-recorded artifact files through the trusted VCS adapter. Status may use an exact publication_id instead of branch/sha; Mizu validates its durable receipt and queries only its recorded branch and published SHA. The publications op lists the project's recorded publication receipts from local durable evidence and contacts no provider; it accepts exact code_digest, branch or published_sha filters and reports the match count, a truncated flag and whether a scan bound stopped the search. The ci_requests op lists the project's recorded CI requests grouped by UTC day from the same local evidence, with the work items each request covered and whether each request is resolved or still unresolved. Never publishes.",
               obj({"op": {"type": "string", "enum": ["status", "log", "comments", "proposals", "acquire", "artifacts", "artifact", "publications", "ci_requests"]},
                    "branch": text(256), "sha": text(64), "publication_id": {"type": "string", "maxLength": 64, "pattern": "[0-9a-f]{64}"}, "id": text(128),
                    "cursor": text(256),
                    "scope": {"type": "string", "enum": ["head", "full"]},
                    "base_sha": text(64), "artifact": text(512),
                    "code_digest": {"type": "string", "maxLength": 64, "pattern": "^[0-9a-f]{64}$"},
                    "published_sha": {"type": "string", "maxLength": 64, "pattern": "^(?:[0-9a-f]{40}|[0-9a-f]{64})$"}}, ("op",))),
    "vcs_request": ("Request one CI run per workflow at an exact published commit through the trusted VCS adapter. The target is a completed publication receipt, never a branch head: the product verifies that every returned run names that commit, refuses any other binding, and records the request with the returned run identities. The role grant and the provider grant are both required, and a recorded request replays instead of spending a second run. A request may also cover work items: each covered item names the revision it is covered at and the checks whose result settles it, and the product registers a receipt-pinned CI wait on that item, so the item's holder keeps the completion decision.",
               obj({"publication_id": {"type": "string", "maxLength": 64, "pattern": "^[0-9a-f]{64}$"},
                    "workflows": array(text(128), 8),
                    "covered": array(obj({"id": {"type": "string", "minLength": 35, "maxLength": 35,
                                                "pattern": "^wi_[0-9a-f]{32}$"},
                                          "rev": {"type": "integer", "minimum": 1},
                                          "checks": array(text(256), 64)},
                                         ("id", "rev", "checks")), 16)},
                   ("publication_id", "workflows"))),
    "vcs_publish": ("Push a branch or open a PR through the trusted VCS adapter. Requires recorded human GO approval for the branch and code digest.",
               obj({"op": {"type": "string", "enum": ["push", "pr"]},
                    "branch": text(256)}, ("op", "branch"))),
    "vcs_retire": ("Retire an owned temporary integration branch at an exact expected sha through the trusted VCS adapter. Requires the configured retire grant; only owned branches retire, everything else is preserved.",
               obj({"branch": text(256), "expected_sha": text(64)}, ("branch", "expected_sha"))),
    "vcs_dispose": ("Close or merge an external proposal at its exact assessed endpoints through the trusted VCS adapter. close and merge retain their existing grants and GO rules. close_if_adopted uses exact id/head_sha/base_sha/target_repo/target_ref/target_sha pins, requires close_grant, and returns the adapter receipt even for a well-formed refusal; it never retries or merges.",
               obj({"op": {"type": "string", "enum": ["close", "merge", "close_if_adopted"]},
                    "id": text(128), "sha": text(64), "base": text(64),
                    "target": text(256), "branch": text(256),
                    "head_sha": text(64), "base_sha": text(64),
                    "target_repo": text(256), "target_ref": text(256),
                    "target_sha": text(64)}, ("op",))),
    "research_read": ("Read this role's own current research state record with its generation. Never touches other roles.",
               obj()),
    "materialize": ("Materialize offline inputs via the trusted host adapter from a declarative spec, or apply one retained receipt byte-exactly. Invoke takes {spec}; apply takes {apply: {run, evidence_id}} and writes retained digest-bound content only when the adapter receipt's policy-checked output exactly matches the spec's output and passes workspace path safety. Never accepts caller path/content, mounts adapter paths, or uses the network from the sandbox.",
               obj({"spec": text(8192), "apply": obj({"run": text(32), "evidence_id": text(32)}, ("run", "evidence_id"))})),
    "research": ("Replace this role's own current research state record with a full new JSON object state at the read generation; stale generations are refused. Audit evidence lands in research-state.json.",
               obj({"state": text(65536), "expected_generation": {"type": "integer", "minimum": 0}}, ("state", "expected_generation"))),
    "finish": ("End this work unit and release its model slot and workspace lock. Call after all other tools. For a timed external wait on a writable role, use outcome=wait and wait_seconds=N; the host persists the wake deadline instead of keeping a command running. Independent new work may wake the role earlier. With check_job, release_checks optionally lists obsolete subscriptions to cancel at a successful finish; other consumers are preserved. Use outcome=wait and wait_for_check=<id> when no independent work remains; release the unit and resume on completion or new input. 'done' on writable roles requires successful verification. "
               "next_profile optionally names the recommended profile for the following unit; next_reason needs next_profile. "
               "The recommendation binds to the finished task inputs and applies only while fresh, per operator selector rules; no extra model call is spent.",
               obj({"outcome": {"type": "string", "enum": ["continue", "wait", "blocked", "done"]},
                    "summary": text(12000), "state": text(32000),
                    "wait_for_check": text(64),
                    "release_checks": array(text(64), 32),
                    "wait_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_SECONDS},
                    "next_profile": text(63), "next_reason": text(256)},
                   ("outcome", "summary"))),
}


def tool_definitions(capabilities: tuple[str, ...]) -> list[dict]:
    return [{"name": "mizu_" + name, "operation": name,
             "description": DEFINITIONS[name][0], "inputSchema": DEFINITIONS[name][1]}
            for name in capabilities]
