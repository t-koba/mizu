"""Upstream-sync trusted argv adapter plus read-only ref injection (M1).

Schema: operator-owned ``[vcs] command`` argv receives one JSON object on
stdin (``{"op": ...}``) and must print one JSON object on stdout.
Bounds: request canonical bytes <= ``max_bytes``; stdout capped at
``max_bytes`` via ``process.run``; at most 4096 refs; ref names 1-512 chars.
Trust: operator-owned host program only; never model-provided argv, never run
in the sandbox. Injected refs are external-untrusted until merged and
verified. Retry/cancellation: single invocation under ``timeout_seconds``;
no shell retry. Evidence: caller binds results to receipts/digests.
Failure: ``Denied`` on unconfigured adapter, oversize request, timeout,
oversize output, nonzero exit, malformed JSON, invalid refs, or unsafe paths.

Ref injection (host side only): ``inject_refs`` writes validated refs as
read-only files under the reserved workspace subtree
``refs/remotes/upstream/*``. ``Snapshots.excluded`` always excludes that
subtree, so injected refs never affect ``code_digest`` even if the model
rewrites them; only a host-side fetch refreshes them. ``files``/``read``
serve them read-only from the project workspace.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .errors import Denied
from .fs import atomic_write, canonical, safe_read
from .process import run

#: Reserved workspace subtree for injected upstream refs. Operators must not
#: keep project source here; snapshots always exclude it (see
#: ``Snapshots.excluded``) so refs never affect ``code_digest``.
REF_PREFIX = ("refs", "remotes", "upstream")
REF_PREFIX_PATH = "/".join(REF_PREFIX)
#: Maximum refs per fetch/injection (matches the adapter shape bound).
MAX_REFS = 4096
#: Ref file content bound for reads (a hex sha plus newline is far smaller).
REF_CONTENT_MAX = 4096

_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")


def invoke(settings: dict, request: dict) -> dict:
    """Invoke the configured adapter once and return its JSON object.

    Schema: ``request`` is a JSON object with a string ``op`` field;
    the adapter must return a JSON object. Bounds: canonical request bytes
    and adapter stdout are each capped at ``max_bytes``; the call is bounded
    by ``timeout_seconds``. Trust: ``settings`` comes from operator config,
    not model output. Failure: ``Denied`` with a short reason; adapter
    output is capped at ``max_bytes`` (combined stdout+stderr via
    ``process.run``) so adapter chatter cannot blow the frame.
    """
    command = settings.get("command", [])
    timeout = settings.get("timeout_seconds", 20)
    maximum = settings.get("max_bytes", 524288)
    if not command:
        raise Denied("Upstream sync is not configured")
    if not isinstance(request, dict) or not isinstance(request.get("op"), str) or not request["op"]:
        raise Denied("VCS adapter request must be an object with a nonempty op string")
    try:
        payload = canonical(request)
    except (TypeError, ValueError) as exc:
        raise Denied("VCS adapter request is not JSON-serializable") from exc
    if len(payload) > maximum:
        raise Denied("VCS adapter request exceeds byte limit")
    result = run(list(command), timeout=timeout, maximum=maximum,
                 input_data=payload + b"\n")
    if result.reason == "timeout":
        raise Denied("Configured VCS program timed out")
    if result.reason == "output_limit":
        raise Denied("VCS adapter response exceeds byte limit")
    if result.exit_code != 0 or result.reason != "exited":
        raise Denied(f"Configured VCS program failed ({result.reason})")
    try:
        data = json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        raise Denied("VCS adapter must return a JSON object") from exc
    if not isinstance(data, dict):
        raise Denied("VCS adapter must return a JSON object")
    return data


def validate_refs(refs) -> dict:
    """Validate a ``{name: sha}`` mapping shared by fetch and injection.

    Schema: names are ``refs/remotes/upstream/``-relative paths (nonempty,
    at most 512 chars, no NUL/newlines/backslashes/colons, no empty/``.``/
    ``..`` components, no leading slash); shas are 40/64 lowercase hex.
    Bounds: at most 4096 entries. Failure: ``Denied``.
    """
    if not isinstance(refs, dict) or len(refs) > MAX_REFS:
        raise Denied("VCS adapter must return a refs object with at most 4096 entries")
    for name, sha in refs.items():
        _check_ref_name(name)
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Denied("Invalid VCS ref digest")
    return dict(refs)


def _check_ref_name(name) -> None:
    if not isinstance(name, str) or not name or len(name) > 512:
        raise Denied("Invalid VCS ref name")
    if "\n" in name or "\x00" in name or "\\" in name or ":" in name:
        raise Denied("Invalid VCS ref name")
    if name.startswith("/"):
        raise Denied("Invalid VCS ref name")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise Denied("Invalid VCS ref name")


def fetch_refs(settings: dict) -> dict:
    """Fetch upstream refs via ``{"op": "fetch"}`` and validate the shape.

    Schema: response ``{"refs": {name: sha}}`` validated by
    ``validate_refs``. Bounds: at most 4096 refs. Trust: refs are
    external-untrusted until merged and verified. Failure: ``Denied``.
    """
    data = invoke(settings, {"op": "fetch"})
    return {"refs": validate_refs(data.get("refs")), "trust": "external-untrusted"}


def split_ref_path(path: str) -> str | None:
    """Return the ref name for a workspace path under the reserved prefix.

    Schema: ``refs/remotes/upstream/<name>`` with a valid ref name, else
    ``None``. Bounds: name rules from ``_check_ref_name``. Trust: pure path
    syntax, no filesystem access. Failure: never raises.
    """
    if not isinstance(path, str):
        return None
    parts = path.split("/")
    if parts[:3] != list(REF_PREFIX) or len(parts) < 4:
        return None
    name = "/".join(parts[3:])
    try:
        _check_ref_name(name)
    except Denied:
        return None
    return name


def _ensure_host_dir(path: Path) -> None:
    """Create a host-side directory level, refusing symlinks and files."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o755)
        return
    except OSError as exc:
        raise Denied(f"Cannot inject upstream refs: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise Denied("Refusing to inject refs through a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise Denied("Upstream ref path is blocked by a non-directory")


def inject_refs(workspace: Path, refs: dict) -> dict:
    """Write validated refs as read-only files under the reserved subtree.

    Schema: ``refs`` is a ``{name: sha}`` mapping per ``validate_refs``;
    each ref lands at ``refs/remotes/upstream/<name>`` holding ``sha``
    plus a newline with mode 0o444. Stale files under the subtree are
    removed so a fetch refreshes the view; stale empty directories are
    left in place. Bounds: at most 4096 refs. Trust: host side only (the
    daemon/operator fetch path), never model-invoked; content stays
    external-untrusted. Retry/cancellation: single pass, no retry.
    Evidence: returned receipt names the prefix and count. Failure:
    ``Denied`` on invalid refs, symlink escape, or blocked paths.
    """
    validated = validate_refs(refs)
    if not workspace.is_dir() or workspace.is_symlink():
        raise Denied("Upstream refs require a real workspace directory")
    base = workspace.joinpath(*REF_PREFIX)
    current = workspace
    for part in REF_PREFIX:
        current = current / part
        _ensure_host_dir(current)
    wanted = set(validated)
    for name in sorted(wanted):
        parent = base
        for part in name.split("/")[:-1]:
            parent = parent / part
            _ensure_host_dir(parent)
        target = base.joinpath(*name.split("/"))
        try:
            existed = target.lstat()
        except FileNotFoundError:
            existed = None
        except OSError as exc:
            raise Denied(f"Cannot inject upstream refs: {exc}") from exc
        if existed is not None and stat.S_ISLNK(existed.st_mode):
            raise Denied("Refusing to overwrite a symlink with upstream ref content")
        atomic_write(target, (validated[name] + "\n").encode(), mode=0o444)
    _prune_stale(base, wanted)
    try:
        have = set(list_refs(workspace))
    except OSError as exc:
        raise Denied(f"Cannot verify injected upstream refs: {exc}") from exc
    if have != wanted:
        raise Denied("Injected upstream refs do not match the validated set")
    return {"injected": len(validated), "prefix": REF_PREFIX_PATH,
            "trust": "external-untrusted"}


def _prune_stale(base: Path, wanted: set[str]) -> None:
    """Remove injected files no longer present, refusing symlink games."""
    for directory, dirs, files in os.walk(base, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for entry in sorted(files):
            target = here / entry
            try:
                if target.is_symlink():
                    raise Denied("Refusing to prune a symlinked upstream ref")
            except OSError as exc:
                raise Denied(f"Cannot prune upstream refs: {exc}") from exc
            rel = target.relative_to(base).as_posix()
            if rel not in wanted:
                try:
                    target.unlink()
                except OSError as exc:
                    raise Denied(f"Cannot prune upstream refs: {exc}") from exc


def list_refs(workspace: Path) -> list[str]:
    """List injected ref names (relative to the reserved prefix).

    Schema: sorted names; missing subtree reads as empty. Bounds: at most
    4096 entries or ``Denied``. Trust: host files; entries failing name
    validation (model-planted garbage) are skipped, never served. Failure:
    ``Denied`` when the subtree overflows; ``OSError`` only for host I/O.
    """
    base = workspace.joinpath(*REF_PREFIX)
    if not base.is_dir() or base.is_symlink():
        return []
    out: list[str] = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for entry in sorted(files):
            target = here / entry
            if target.is_symlink():
                continue
            rel = target.relative_to(base).as_posix()
            try:
                _check_ref_name(rel)
            except Denied:
                continue
            out.append(rel)
    if len(out) > MAX_REFS:
        raise Denied("Too many injected upstream refs")
    return sorted(out)


def read_ref(workspace: Path, name: str) -> str:
    """Read one injected ref sha as text (read-only model view helper).

    Schema: ``name`` validated per ``_check_ref_name``; content must be a
    40/64 lowercase hex sha (trailing newline tolerated). Bounds: file
    capped at 4096 bytes via ``safe_read`` (no symlinks/hardlinks).
    Failure: ``Denied`` on bad names, unsafe paths, or malformed content.
    """
    _check_ref_name(name)
    data = safe_read(workspace, REF_PREFIX_PATH + "/" + name, REF_CONTENT_MAX)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Denied("Invalid upstream ref content") from exc
    if not _SHA.fullmatch(text.strip()):
        raise Denied("Invalid upstream ref content")
    return text.strip()


#: Read-only adapter operations served by ``vcs_read``. Publishing
#: operations (``push``/``pr``) are never served through this path.
READ_OPS = frozenset({"status", "log", "comments"})
#: Mutating adapter operations served by ``vcs_publish`` behind a recorded
#: human ``GO <branch>`` approval bound to branch and code digest.
PUBLISH_OPS = frozenset({"push", "pr"})

#: Approval body line binding an insight to the exact pushed tree.
#: Example body line: ``digest: <64 lowercase hex>``.
GO_DIGEST_RE = re.compile(r"^digest:\s*([0-9a-f]{64})\s*$", re.M)


def go_title(branch: str) -> str:
    """Return the required human-approval insight title for a branch."""
    return "GO " + branch


def parse_go_digest(body: str) -> str | None:
    """Return the ``digest:`` line from an approval body, else ``None``."""
    if not isinstance(body, str):
        return None
    match = GO_DIGEST_RE.search(body)
    return match.group(1) if match else None


def check_branch(branch: str) -> str:
    """Validate a publish/read branch name (same rules as ref names)."""
    _check_ref_name(branch)
    if len(branch) > 256:
        raise Denied("Invalid VCS branch name")
    return branch


#: Insight source and decision run marking an operator-recorded approval.
#: Model ``submit_insight`` fixes source to the role name and model ``decide``
#: fixes run to the run directory name, so neither can mint these values.
OPERATOR_SOURCE = "operator"
OPERATOR_RUN = "operator"


def require_go_approval(project, branch: str, code_digest: str) -> dict:
    """Require a recorded human ``GO <branch>`` approval for this digest.

    Schema: ``branch`` per ``check_branch``; ``code_digest`` 64 hex of the
    exact tree being published (workspace capture, not model supplied).
    Trust: local insight inbox plus ``decisions/`` records; the approval
    insight must carry source ``operator`` (``mizu insight submit``) and the
    ``accept`` decision must carry run ``operator`` (``mizu insight decide``).
    Model-submitted insights (source is the role name) and model decisions
    (run is the run directory name) never satisfy this gate. Use a dedicated
    publisher role without ``submit_insight``/``decide`` (refused together
    with ``vcs_publish`` at config load) for defense in depth.
    Retry/cancellation: pure local reads, no retry. Evidence: returned
    ``{"insight": id, "branch": ..., "code_digest": ...}`` names the
    approval bound to this publication. Failure: ``Denied`` when no matching
    title exists, when the digest line is missing/stale, when the source or
    decision run is not the operator channel, or when the matching
    record is undecided or not ``accept``. Stale digests are refused even
    when an older approval exists.
    """
    from .fs import DIGEST as _DIGEST
    check_branch(branch)
    if not isinstance(code_digest, str) or not _DIGEST.fullmatch(code_digest):
        raise Denied("Invalid code digest for publication approval")
    want = go_title(branch)
    inbox = project.root / "inbox"
    saw_title = False
    saw_stale = False
    saw_undecided = False
    saw_refused = False
    saw_forged = False
    try:
        paths = sorted(inbox.glob("*.json"))
    except OSError as exc:
        raise Denied(f"Publication approval is unavailable: {exc}") from exc
    for path in paths:
        if path.is_symlink():
            continue
        try:
            item = json.loads(path.read_bytes())
            if not isinstance(item, dict) or item.get("title") != want:
                continue
        except (OSError, ValueError):
            continue
        saw_title = True
        if item.get("source") != OPERATOR_SOURCE:
            saw_forged = True
            continue
        if parse_go_digest(item.get("body", "")) != code_digest:
            saw_stale = True
            continue
        insight_id = item.get("id")
        if not isinstance(insight_id, str) or not insight_id:
            continue
        decision_path = project.root / "decisions" / f"{insight_id}.json"
        if decision_path.is_symlink():
            saw_undecided = True
            continue
        try:
            decision = json.loads(decision_path.read_bytes())
        except (OSError, ValueError):
            saw_undecided = True
            continue
        if not isinstance(decision, dict) or decision.get("id") != insight_id:
            saw_undecided = True
            continue
        if decision.get("action") == "accept" and decision.get("run") == OPERATOR_RUN:
            return {"insight": insight_id, "branch": branch,
                    "code_digest": code_digest}
        if decision.get("action") == "accept":
            saw_forged = True
            continue
        saw_refused = True
    if saw_forged:
        raise Denied("Publication approval must come from the operator channel")
    if saw_refused or saw_undecided:
        raise Denied("Publication approval is not accepted for this code digest")
    if saw_stale:
        raise Denied("Recorded human approval is stale for this code digest")
    if saw_title:
        raise Denied("Publication approval is not accepted for this code digest")
    raise Denied("External publication requires recorded human approval")


def read_via(settings: dict, op: str, params: dict) -> dict:
    """Invoke a read-only adapter operation (``status``/``log``/``comments``).

    Schema: ``params`` must include ``branch``; optional ``sha`` passes
    through when given. Bounds: same ``max_bytes``/timeout contract as
    ``invoke``. Trust: operator-owned adapter; results are
    external-untrusted. Failure: ``Denied`` on publish ops, bad branch, or
    any adapter contract violation. Never publishes.
    """
    if op not in READ_OPS:
        raise Denied("vcs_read cannot publish; unknown or mutating operation")
    if not isinstance(params, dict):
        raise Denied("Invalid VCS read parameters")
    branch = params.get("branch")
    check_branch(branch)
    request = {"op": op, "branch": branch}
    sha = params.get("sha")
    if sha is not None:
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Denied("Invalid VCS sha filter")
        request["sha"] = sha
    data = invoke(settings, request)
    if op == "status":
        return {"op": op, "branch": branch, "checks": parse_status_checks(data),
                "trust": "external-untrusted"}
    return {**data, "trust": "external-untrusted"}


#: Maximum CI checks per status response (adapter payload already capped).
MAX_CHECKS = 1024


def parse_status_checks(data: dict) -> list:
    """Validate a ``status`` adapter response into normalized check rows.

    Schema: ``{"checks": [{check, state, sha, url?}]}``; ``check`` 1-256
    chars without newline/NUL, ``state`` 1-64 chars without newline/NUL,
    ``sha`` 40/64 lowercase hex, optional ``url`` at most 4096 chars
    without newline/NUL. Bounds: at most 1024 checks. Trust: adapter
    facts stay external-untrusted; this only validates shape. Failure:
    ``Denied`` on missing/malformed shapes.
    """
    if not isinstance(data, dict):
        raise Denied("VCS status must be a JSON object")
    checks = data.get("checks")
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise Denied("VCS status must carry a checks array with at most 1024 entries")
    out = []
    for entry in checks:
        if not isinstance(entry, dict) or set(entry) - {"check", "state", "sha", "url"}:
            raise Denied("Invalid VCS status check entry")
        check = entry.get("check")
        state = entry.get("state")
        sha = entry.get("sha")
        url = entry.get("url", "")
        if (not isinstance(check, str) or not check or len(check) > 256
                or "\n" in check or "\x00" in check):
            raise Denied("Invalid CI check name")
        if (not isinstance(state, str) or not state or len(state) > 64
                or "\n" in state or "\x00" in state):
            raise Denied("Invalid CI check state")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Denied("Invalid VCS sha for CI insight")
        if not isinstance(url, str) or len(url) > 4096 or "\x00" in url or "\n" in url:
            raise Denied("Invalid CI log URL")
        out.append({"check": check, "state": state, "sha": sha, "url": url})
    return out


def publish_via(settings: dict, op: str, params: dict) -> dict:
    """Invoke a mutating adapter operation (``push``/``pr``) after approval.

    Schema: ``params`` must include ``branch`` and ``code_digest`` (64 hex
    of the approved tree). The request sends ``digest`` alongside ``op``
    and ``branch``; the adapter must verify the pushed tree matches it
    before pushing and echo the same ``digest`` in its response. Bounds:
    same contract as ``invoke``. Trust: operator-owned adapter only; the
    caller must have passed ``require_go_approval`` first (this helper
    checks the op gate, not the human record). Failure: ``Denied`` on
    read-only/unknown ops, bad digest, or adapter violations including a
    missing/mismatched digest echo.
    """
    from .fs import DIGEST as _DIGEST
    if op not in PUBLISH_OPS:
        raise Denied("vcs_publish cannot publish this operation")
    if not isinstance(params, dict):
        raise Denied("Invalid VCS publish parameters")
    branch = params.get("branch")
    check_branch(branch)
    code_digest = params.get("code_digest")
    if not isinstance(code_digest, str) or not _DIGEST.fullmatch(code_digest):
        raise Denied("Invalid code digest for publication")
    data = invoke(settings, {"op": op, "branch": branch, "digest": code_digest})
    if not isinstance(data, dict):
        raise Denied("VCS adapter must return a JSON object")
    if data.get("digest") != code_digest:
        raise Denied("VCS adapter must confirm the pushed code digest")
    return {**data, "trust": "external-untrusted"}


def ci_insight_id(branch: str, sha: str, check: str) -> str:
    """Derive a stable, deduplicating insight ID for a CI failure."""
    from .fs import digest as _digest, canonical as _canonical
    check_branch(branch)
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise Denied("Invalid VCS sha for CI insight")
    if not isinstance(check, str) or not check or len(check) > 256 or "\n" in check or "\x00" in check:
        raise Denied("Invalid CI check name")
    return "ci-" + _digest(_canonical({"branch": branch, "sha": sha, "check": check}))[:32]


def record_ci_result(project, *, branch: str, sha: str, check: str,
                     state: str, url: str = "", run: str | None = None) -> dict:
    """Record a CI failure as an insight; deduplicate repeats, ignore passes.

    Schema: ``state`` is ``"failure"`` (record) or anything else
    (ignored). ``url`` is an optional bounded log link, validated but not
    stored: the persisted body covers only branch/sha/check so repeats with
    a varying per-run log URL resubmit identical content and return the
    existing record. Bounds: title <= 200 chars, body within the insight
    byte limit. Trust: adapter-derived facts labeled external-untrusted in
    the body; the stable ID lets repeats return the existing record instead
    of spamming the inbox. The latest log URL stays available via
    ``vcs_read`` ``status``. Failure: ``Denied`` on bad names or bad URLs;
    adapter content never raises beyond validation.
    """
    if state != "failure":
        return {"recorded": False, "state": state}
    if not isinstance(url, str) or len(url) > 4096 or "\x00" in url or "\n" in url:
        raise Denied("Invalid CI log URL")
    insight_id = ci_insight_id(branch, sha, check)
    title = f"CI {check} failed on {branch}"
    if len(title) > 200:
        title = title[:200]
    body = (f"CI check '{check}' failed on branch '{branch}' at {sha}.\n"
            f"trust: external-untrusted\n")
    record = project.insights.submit(source="vcs", title=title, body=body,
                                     base_snapshot=None, run=run,
                                     insight_id=insight_id)
    return {"recorded": True, "id": record["id"], "title": title}
