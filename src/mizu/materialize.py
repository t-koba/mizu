"""Generic offline-input materialization via a trusted host adapter.

Apply mode (same grant): a retained `spec` receipt whose JSON spec carries
a top-level string `output` may be applied byte-exactly to that exact path
only when the retained adapter `result` carries the same policy-checked
`output`. Output authorization lives in the host adapter and its ops.toml
`[materialize] output_allowed` policy, never in Mizu configuration; the
adapter returns the checked path in its result. The caller supplies no path
or content; the destination comes only from the retained spec/result pair,
the bytes only from the retained digest-bound content. Writes
are atomic via `fs.safe_write` (POSIX openat/O_NOFOLLOW, portable
resolve-and-check fallback), never create parents, and refuse excluded,
symlinked, hardlinked or special destinations. Failed validation never
touches the destination. Results stay external-untrusted; apply never
verifies, commits or publishes.

Schema: operator-owned `[materialize] command` argv receives one JSON object
on stdin (`{"spec": ...}`) and must print one JSON object on stdout.
Bounds: spec at most 8192 chars; canonical request and adapter stdout each
capped at `max_bytes`; UTF-8 content at most `max_bytes` bytes; cache identifier
at most 256 chars; output path at most 4096 chars. Trust: operator-owned host program only; never
model-provided argv, never run in the sandbox. Results stay
external-untrusted until applied as workspace edits and verified.
Retry/cancellation: single invocation under `timeout_seconds`; no shell retry.
Evidence: caller binds results to digests/receipts.
Failure: `Denied` on unconfigured adapter, oversize request, timeout,
oversize output, nonzero exit, malformed JSON, or digest mismatch. A normally
exited nonzero adapter may attach the configured `diagnostic_bytes` tail of
stderr, labelled external-untrusted; zero disables it.

The adapter warms host caches already mounted read-only via static
`[sandbox] mounts` (e.g. a cargo/npm cache directory) and/or returns
digest-bound text content (e.g. an updated lockfile) that can be applied as
an ordinary workspace edit. Dynamic per-request mounts and image switching
are explicitly out of scope: the product never mounts arbitrary adapter paths.

Ecosystem logic (cargo/npm/rustup, registry allowlists, layer builds)
lives in the operator adapter and its policy, never here.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .errors import Denied
from .fs import DIGEST, canonical, digest, relative_parts, safe_read, safe_write, write_json
from .process import (
    ADAPTER_DIAGNOSTIC_BYTES_DEFAULT, adapter_failure_diagnostic, run,
)

#: Declarative request bound: a description of desired inputs, not a script.
SPEC_MAX = 8192
#: Cache identifier bound: names a warmed location for audit, never a mount.
CACHE_ID_MAX = 256


def invoke(settings: dict, spec: str, *, cancel=lambda: False) -> dict:
    """Invoke the configured adapter once and return its validated receipt.

    Schema: `spec` is a nonempty declarative description (opaque to the
    product, interpreted by the operator adapter). Response
    `{"content": str, "digest": 64 hex, "cache": str optional,
    "output": str optional}` where `digest` must equal sha256 over
    `fs.canonical(content)` recomputed locally. `cache` names the warmed
    location for audit only. `output` is the adapter policy-checked exact
    workspace-relative destination; it is preserved verbatim in the receipt.
    Cache-only/older responses may omit `output` and still invoke
    successfully, but apply then fails closed. On a normal
    nonzero exit, the configured stderr tail is model-visible and untrusted.
    """
    command = settings.get("command", [])
    timeout = settings.get("timeout_seconds", 20)
    maximum = settings.get("max_bytes", 524288)
    if not command:
        raise Denied("Dependency materialization is not configured")
    if not isinstance(spec, str) or not spec.strip() or len(spec) > SPEC_MAX:
        raise Denied("Materialize spec must be a nonempty description of at most 8192 chars")
    if "\x00" in spec:
        raise Denied("Materialize spec must not contain NUL")
    try:
        payload = canonical({"spec": spec})
    except (TypeError, ValueError, UnicodeError) as exc:
        raise Denied("Materialize request is not JSON-serializable") from exc
    if len(payload) > maximum:
        raise Denied("Materialize request exceeds byte limit")
    result = run(list(command), timeout=timeout, maximum=maximum,
                 input_data=payload + b"\n", cancel=cancel)
    if result.reason == "timeout":
        raise Denied("Configured materialize program timed out")
    if result.reason == "output_limit":
        raise Denied("Materialize adapter response exceeds byte limit")
    if result.exit_code != 0 or result.reason != "exited":
        diagnostic = adapter_failure_diagnostic(
            result, source="materialize", maximum_bytes=settings.get(
                "diagnostic_bytes", ADAPTER_DIAGNOSTIC_BYTES_DEFAULT))
        raise Denied(f"Configured materialize program failed ({result.reason}){diagnostic}")
    try:
        data = _loads(result.stdout)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise Denied("Materialize adapter must return a JSON object") from exc
    if not isinstance(data, dict):
        raise Denied("Materialize adapter must return a JSON object")
    content = data.get("content")
    echoed = data.get("digest")
    if not isinstance(content, str) or _utf8_size(content) > maximum:
        raise Denied("Materialize adapter must return content within the byte limit")
    if not isinstance(echoed, str) or len(echoed) != 64:
        raise Denied("Materialize adapter must confirm the content digest")
    if echoed != digest(canonical(content)):
        raise Denied("Materialized content digest mismatch")
    cache = data.get("cache", "")
    if cache is None:
        cache = ""
    if not isinstance(cache, str) or len(cache) > CACHE_ID_MAX:
        raise Denied("Invalid materialize cache identifier")
    if "\x00" in cache or "\n" in cache:
        raise Denied("Invalid materialize cache identifier")
    _utf8_size(cache)
    output = data.get("output", "")
    if output is None:
        output = ""
    if not isinstance(output, str):
        raise Denied("Materialize adapter must return a valid output path")
    if output:
        if len(output) > _OUTPUT_MAX:
            raise Denied("Materialize adapter must return a valid output path")
        if "\x00" in output or "\n" in output:
            raise Denied("Materialize adapter must return a valid output path")
        _utf8_size(output)
    return {"content": content, "digest": echoed, "cache": cache,
            "output": output, "trust": "external-untrusted"}

#: Retained-evidence locator: full lowercase 32-hex run/evidence UUIDs only.
_EVIDENCE_UUID = re.compile(r"[0-9a-f]{32}\Z")
#: started.json bound for apply reads (same as runtime evidence reads).
_STARTED_MAX = 65536
#: Longest allowed output path spelling (exact workspace-relative path).
_OUTPUT_MAX = 4096

def _no_duplicates(pairs: list) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("Duplicate key")
        seen[key] = value
    return seen


def _reject_nonfinite_constant(value: str):
    raise ValueError("Non-finite JSON value")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _loads(raw: str | bytes):
    return json.loads(raw, object_pairs_hook=_no_duplicates,
                      parse_constant=_reject_nonfinite_constant,
                      parse_float=_finite_float)


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise Denied("Materialize text must be valid UTF-8") from exc


def parse_output(spec: str) -> str:
    """Return the exact workspace-relative output path from a JSON spec.

    The spec is otherwise opaque: any extra keys are ignored. Only a
    top-level string `output` selects the apply destination. Malformed
    JSON, duplicate keys, non-finite numbers, a non-object document, or a missing/non-string
    output is `Denied`.
    """
    if not isinstance(spec, str) or not spec or len(spec) > SPEC_MAX:
        raise Denied("Materialize spec carries no applicable output")
    if "\x00" in spec:
        raise Denied("Materialize spec carries no applicable output")
    try:
        _utf8_size(spec)
        data = _loads(spec)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise Denied("Materialize spec carries no applicable output") from exc
    if not isinstance(data, dict):
        raise Denied("Materialize spec carries no applicable output")
    output = data.get("output")
    if not isinstance(output, str) or not output or len(output) > _OUTPUT_MAX:
        raise Denied("Materialize spec carries no allowed output path")
    if "\x00" in output or "\n" in output:
        raise Denied("Materialize spec carries no allowed output path")
    _utf8_size(output)
    try:
        relative_parts(output)
    except Denied as exc:
        raise Denied("Materialize spec carries no allowed output path") from exc
    return output


def load_source(settings: dict, project_root: Path, run: str, evidence_id: str,
                role_name: str, file_bytes: int) -> dict:
    """Validate a retained materialize receipt and return its apply inputs.

    Reads `runs/<run>/started.json` and `runs/<run>/materialize/<id>.json`
    with existing safe bounded reads under the current project only.
    Requires same-project/same-role binding (started.json run/role), the
    exact evidence id/path, `external-untrusted` trust, and a digest that
    recomputes over `fs.canonical(result.content)` and matches both the
    top-level and result digests. Old receipts without top-level
    run/role/kind stay valid through their started.json binding; new
    receipts must bind when those fields are present. The retained adapter
    `result` must carry a policy-checked `output` that equals the top-level
    string `output` in the same retained JSON spec exactly; absent, empty,
    mismatched, or path-unsafe outputs fail closed. Returns `{output,
    content, digest, cache, source_run, source_evidence}`; the bytes written
    are `content.encode("utf-8")`.
    """
    if (not isinstance(run, str) or not isinstance(evidence_id, str)
            or not _EVIDENCE_UUID.fullmatch(run)
            or not _EVIDENCE_UUID.fullmatch(evidence_id)):
        raise Denied("Materialize apply requires full run and evidence UUIDs")
    if not isinstance(role_name, str) or not role_name:
        raise Denied("Materialize apply requires a role binding")
    maximum = settings.get("max_bytes", 524288)
    if type(file_bytes) is not int or file_bytes <= 0:
        raise Denied("Materialize apply is unavailable")
    try:
        started_raw = safe_read(project_root, f"runs/{run}/started.json",
                                min(file_bytes, _STARTED_MAX)).decode("utf-8")
        started = _loads(started_raw)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise Denied("Corrupt retained materialize evidence") from exc
    if (not isinstance(started, dict) or started.get("run") != run
            or started.get("role") != role_name):
        raise Denied("Materialize evidence run or role mismatch")
    try:
        record_raw = safe_read(project_root, f"runs/{run}/materialize/{evidence_id}.json",
                               file_bytes).decode("utf-8")
        record = _loads(record_raw)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise Denied("Corrupt retained materialize evidence") from exc
    if not isinstance(record, dict):
        raise Denied("Corrupt retained materialize evidence")
    if record.get("evidence_id") != evidence_id:
        raise Denied("Materialize evidence identity mismatch")
    if record.get("evidence") != f"materialize/{evidence_id}.json":
        raise Denied("Materialize evidence path mismatch")
    kind = record.get("kind", "materialize")
    if kind != "materialize":
        raise Denied("Materialize evidence kind mismatch")
    if record.get("trust") != "external-untrusted":
        raise Denied("Materialize evidence trust mismatch")
    if "run" in record and record["run"] != run:
        raise Denied("Materialize evidence run mismatch")
    if "role" in record and record["role"] != role_name:
        raise Denied("Materialize evidence role mismatch")
    result = record.get("result")
    if not isinstance(result, dict):
        raise Denied("Materialize evidence has no digest-bound content")
    content = result.get("content")
    result_digest = result.get("digest")
    top_digest = record.get("digest")
    if not isinstance(content, str) or _utf8_size(content) > maximum:
        raise Denied("Materialize evidence content exceeds the byte limit")
    if (not isinstance(result_digest, str) or not DIGEST.fullmatch(result_digest)
            or not isinstance(top_digest, str) or not DIGEST.fullmatch(top_digest)):
        raise Denied("Materialize evidence digest mismatch")
    expected = digest(canonical(content))
    if result_digest != expected or top_digest != expected:
        raise Denied("Materialize evidence digest mismatch")
    if result.get("trust", "external-untrusted") != "external-untrusted":
        raise Denied("Materialize evidence trust mismatch")
    result_output = result.get("output", "")
    if result_output is None:
        result_output = ""
    if not isinstance(result_output, str) or not result_output:
        raise Denied("Materialize receipt carries no adapter output")
    if len(result_output) > _OUTPUT_MAX:
        raise Denied("Materialize output path is not allowed")
    if "\x00" in result_output or "\n" in result_output:
        raise Denied("Materialize output path is not allowed")
    _utf8_size(result_output)
    try:
        relative_parts(result_output)
    except Denied as exc:
        raise Denied("Materialize output path is not allowed") from exc
    spec = record.get("spec")
    if not isinstance(spec, str):
        raise Denied("Materialize spec carries no allowed output path")
    output = parse_output(spec)
    if result_output != output:
        raise Denied("Materialize output mismatch")
    cache = result.get("cache", record.get("cache", ""))
    if cache is None:
        cache = ""
    if not isinstance(cache, str) or len(cache) > CACHE_ID_MAX:
        raise Denied("Invalid materialize cache identifier")
    if "\x00" in cache or "\n" in cache:
        raise Denied("Invalid materialize cache identifier")
    _utf8_size(cache)
    return {"output": output, "content": content, "digest": expected,
            "cache": cache, "source_run": run,
            "source_evidence": f"materialize/{evidence_id}.json"}


def op_materialize(context, args: dict) -> dict:
    """Run the single `materialize` tool grant in its two exclusive forms.

    `{spec}` invokes the trusted host adapter (warming pre-mounted
    read-only caches and/or returning digest-bound content plus the
    policy-checked output), while `{apply: {run, evidence_id}}` writes
    one retained receipt byte-exactly only when the retained adapter
    `result.output` equals the retained JSON spec `output` exactly.
    Invoke needs a visible workspace; apply needs a writable one.
    The mechanism never mounts adapter paths and never grants network
    to the sandbox. Fail closed without configuration or grant.
    """
    has_spec = "spec" in args
    has_apply = "apply" in args
    if has_spec == has_apply:
        raise Denied("Supply spec or apply, not both or neither")
    if has_spec:
        if context.role.workspace == "none":
            raise Denied("This role has no workspace")
        spec = args["spec"]
        if not isinstance(spec, str):
            raise Denied("Materialize spec must be a string")
        try:
            data = invoke(context.config.materialize, spec,
                          cancel=context.tools_cancelled)
        except OSError as exc:
            raise Denied(f"Materialization is unavailable: {exc}") from exc
        evidence_id, evidence = context._new_invocation_evidence("materialize")
        record = {"run": context.run_dir.name, "role": context.role.name,
                  "kind": "materialize", "evidence_id": evidence_id,
                  "evidence": evidence, "spec": spec, "digest": data["digest"],
                  "cache": data["cache"], "trust": "external-untrusted",
                  "result": data}
        write_json(context.run_dir / evidence, record)
        return record
    # Apply mode: caller supplies only retained locators; the path and
    # bytes come solely from the retained receipt. Failed validation
    # never touches the destination; success stages an audit receipt.
    if context.role.workspace != "write":
        raise Denied("Materialize apply requires a writable workspace")
    apply = args["apply"]
    if (not isinstance(apply, dict) or set(apply) != {"run", "evidence_id"}
            or not isinstance(apply["run"], str)
            or not isinstance(apply["evidence_id"], str)):
        raise Denied("Materialize apply requires run and evidence_id")
    try:
        source = load_source(
            context.config.materialize, context.project.root,
            apply["run"], apply["evidence_id"], context.role.name,
            context.config.limits.file_bytes)
    except OSError as exc:
        raise Denied(f"Materialize apply is unavailable: {exc}") from exc
    path = source["output"]
    if context.project.snapshots.excluded(path):
        raise Denied("Materialize output path is not allowed")
    data = source["content"].encode("utf-8")
    if len(data) > context.config.limits.file_bytes:
        raise Denied("Materialize evidence content exceeds the byte limit")
    safe_write(context.workspace, path, data)
    evidence_id, evidence = context._new_invocation_evidence("materialize")
    record = {"kind": "materialize-apply",
              "run": context.run_dir.name, "role": context.role.name,
              "source_run": source["source_run"],
              "source_evidence": source["source_evidence"],
              "path": path, "digest": source["digest"],
              "bytes": len(data), "cache": source["cache"],
              "trust": "external-untrusted",
              "evidence_id": evidence_id, "evidence": evidence}
    write_json(context.run_dir / evidence, record)
    return record
