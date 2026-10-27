"""Strict TOML configuration. Mechanisms interpret grants, not role prose."""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import tomllib
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError, Denied
from .fs import ID, relative_parts
from .process import ADAPTER_DIAGNOSTIC_BYTES_DEFAULT, ADAPTER_DIAGNOSTIC_BYTES_MAX
CAPABILITIES = frozenset({"diff", "files", "read", "exec", "experiment", "verify", "check_job", "fetch", "probe",
                          "search", "insights", "decide", "submit_insight", "revise_insight", "withdraw_insight", "withdraw_decided", "attach_evidence", "consult", "delegate",
                          "report", "finish", "sync", "vcs_read", "vcs_publish",
                          "vcs_retire", "vcs_dispose", "vcs_request", "research_read", "research",
                          "materialize", "command_evidence_read", "experiment_evidence_read",
                          "work_items_read", "work_items_create", "work_items_transition", "work_items_legacy_read", "work_items_reconcile"})
ENGINES = ("pi", "pi-durable", "codex", "claude")
#: Role names that collide with insight sources owned by the host/operator
#: channel (operator CLI, vcs CI helper, editor outbox). Model
#: `submit_insight` fixes source to the role name, so these are refused
#: at config load to keep the operator channel unforgeable.
RESERVED_ROLE_NAMES = frozenset({"operator", "vcs", "editor"})
MAX_RESOURCES = 64
#: Fixed bound for explicitly added CI wake branches beyond `ci_branch`.
MAX_CI_WAKE_EXTRA_BRANCHES = 64
#: Protocol wait bound mirrored in protocol.finish.wait_seconds; keep both at 86400.
MAX_WAIT_SECONDS = 86400
#: Free-text state changes are supported as an explicit progress signal but
#: do not qualify by default because paraphrasing alone is not substantive work.
DEFAULT_PROGRESS_SIGNALS = ("tool", "code", "decision", "insight", "input")
#: Shared-fragment `include` bounds (fixed mechanism, not knobs per ADR-006).
MAX_INCLUDE_DEPTH = 8
MAX_INCLUDE_FILES = 32
MAX_INCLUDE_BYTES = 1048576
MAX_INCLUDE_ENTRIES = 32
#: Composed role-policy bounds (fixed mechanism, not knobs): at most 16
#: Markdown parts, each at most 64 KiB, 256 KiB total composed text.
MAX_ROLE_POLICY_PARTS = 16
MAX_ROLE_POLICY_BYTES = 65536
MAX_ROLE_POLICY_TOTAL = 262144
#: Bounded event-only recovery poll cadence (Task Scheduler minimum/bounds):
#: an unscheduled watched role rechecks pending event inputs this often when
#: edge triggers are coalesced or lost. Fixed default; per-role override only.
EVENT_POLL_SECONDS_DEFAULT = 60
EVENT_POLL_SECONDS_MIN = 60
EVENT_POLL_SECONDS_MAX = 31 * 86400


def _tz_database_missing() -> bool:
    try:
        ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        return True
    return False


def resolve_timezone(name: str):
    """Return tzinfo for a configured zone name.

    UTC is unambiguous without a database (zero offset, no DST), so it stays
    valid where the platform ships none (Windows without tzdata). Any other
    name requires the database; a missing database is reported as such
    instead of blaming the key, and unknown keys are still refused.
    """
    if name == "local":
        from datetime import datetime
        return datetime.now().astimezone().tzinfo
    from datetime import timezone as _utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if _tz_database_missing():
            if name == "UTC":
                return _utc.utc
            raise ConfigError("No timezone database on this platform; only UTC is "
                              "available (install tzdata for other zones)") from exc
        raise ConfigError(f"Unknown timezone: {name}") from exc


def keys(data: dict, allowed: set[str], where: str) -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"Unknown keys in {where}: {', '.join(sorted(unknown))}")


def number(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{name} must be an integer in [{low}, {high}]")
    return value


def string(value: Any, name: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ConfigError(f"{name} must be a string without NUL")
    return value


def strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) or "\x00" in x for x in value):
        raise ConfigError(f"{name} must be an array of strings")
    return tuple(value)


def _vcs_prefix(value, name):
    """Validate one owned-branch namespace prefix (literal, no wildcards)."""
    if (not isinstance(value, str) or not value or len(value) > 256
            or value.startswith("/") or any(c.isspace() or c in "\x00\\:" for c in value)):
        raise ConfigError(f"{name} must hold nonempty prefixes without whitespace (at most 256 chars)")
    parts = value.split("/")
    if any(part in (".", "..") for part in parts):
        raise ConfigError(f"{name} must not hold dot or dot-dot components")
    if any(part == "" for part in parts[:-1]):
        raise ConfigError(f"{name} must not hold empty components except one trailing slash")


def _lineage_target(value, name):
    """Validate the optional expected promotion-target descriptor."""
    from .lineage import check_target
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    allowed = {"repository", "remote", "target_ref", "version"}
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"Unknown keys in {name}: {', '.join(sorted(unknown))}")
    missing = allowed - set(value)
    if missing:
        raise ConfigError(f"Missing keys in {name}: {', '.join(sorted(missing))}")
    if check_target(value) is None:
        raise ConfigError(f"{name} must hold a 64-hex repository, a safe remote name, a valid full refs/heads/<branch> ref and a version in [1, 1073741824]; no implicit origin/main")
    return {"repository": value["repository"], "remote": value["remote"], "target_ref": value["target_ref"], "version": value["version"]}


def boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be boolean")
    return value


def expand(value: str, env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in env or not env[name]:
            raise ConfigError(f"Required environment variable is unset: {name}")
        return env[name]
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def path_value(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(expand(value)))
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _read_toml_bounded(path: Path, budget: list[int]) -> dict:
    """Read one TOML file against the shared include byte budget."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot load configuration: {exc}") from exc
    budget[0] += len(raw)
    if budget[0] > MAX_INCLUDE_BYTES:
        raise ConfigError(f"Included configuration exceeds {MAX_INCLUDE_BYTES} bytes")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load configuration: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a table")
    return data


def _include_target(includer: Path, entry: str) -> Path:
    """Resolve one include entry relative to its includer without symlink escape."""
    if not isinstance(entry, str) or not entry or len(entry) > 4096:
        raise ConfigError("include entries must be nonempty paths of at most 4096 chars")
    if "\x00" in entry or "\n" in entry:
        raise ConfigError("include entries must not contain NUL or newlines")
    if entry.startswith("~") or "${" in entry:
        raise ConfigError("include entries are literal relative paths without ~ or ${VAR}")
    candidate = Path(entry)
    if candidate.anchor or PureWindowsPath(entry).anchor:
        raise ConfigError("include entries must be relative to the including file")
    if not entry.endswith(".toml"):
        raise ConfigError("include entries must reference .toml files")
    joined = includer.parent / candidate
    # Refuse symlink escapes: no symlink in the written path at or below
    # the includer directory, nor in the final target. Ancestors above the
    # includer directory (e.g. platform temp symlinks) are out of scope;
    # non-existent prefixes cannot be symlinks and are skipped.
    try:
        stack = list(includer.parent.parts)
        for part in candidate.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if len(stack) > 1:
                    stack.pop()
                continue
            stack.append(part)
            if Path(*stack).is_symlink():
                raise ConfigError(f"include target escapes via symlink: {entry}")
        if joined.is_symlink():
            raise ConfigError(f"include target escapes via symlink: {entry}")
        if not joined.is_file():
            raise ConfigError(f"Included file not found: {entry}")
    except OSError as exc:
        raise ConfigError(f"Cannot load configuration: {exc}") from exc
    return joined.resolve()


def _merge_into(base: dict, fragment: dict, origin: Path, origins: dict[str, Path]) -> None:
    """Deep-merge fragment into base; duplicate leaf paths name both files."""
    def walk(dst: dict, src: dict, prefix: str) -> None:
        for key, value in src.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if key in dst:
                current = dst[key]
                if isinstance(current, dict) and isinstance(value, dict):
                    walk(current, value, dotted)
                else:
                    first = origins.get(dotted, origin)
                    raise ConfigError(
                        f"Duplicate configuration key '{dotted}' defined in "
                        f"{first} and {origin}")
            else:
                dst[key] = value
                if not isinstance(value, dict):
                    origins[dotted] = origin
                else:
                    def mark(node: dict, path: str) -> None:
                        for sub, val in node.items():
                            sub_path = f"{path}.{sub}" if path else str(sub)
                            if isinstance(val, dict):
                                mark(val, sub_path)
                            elif sub_path not in origins:
                                origins[sub_path] = origin
                    mark(value, dotted)
    walk(base, fragment, "")


def _load_merged(top: Path) -> dict:
    """Load the top file plus its transitive `include` graph into one dict."""
    budget = [0]
    merged: dict = {}
    origins: dict[str, Path] = {}
    visited: set[Path] = set()

    def visit(path: Path, depth: int, chain: tuple[Path, ...]) -> None:
        resolved = path.resolve()
        if resolved in chain:
            raise ConfigError(f"Include cycle detected: {' -> '.join(str(p) for p in (*chain, resolved))}")
        if resolved in visited:
            return
        if len(visited) >= MAX_INCLUDE_FILES:
            raise ConfigError(f"Too many included files (max {MAX_INCLUDE_FILES})")
        if depth > MAX_INCLUDE_DEPTH:
            raise ConfigError(f"Include depth exceeds {MAX_INCLUDE_DEPTH}")
        visited.add(resolved)
        data = _read_toml_bounded(resolved, budget)
        raw_include = data.pop("include", [])
        if not isinstance(raw_include, list) or any(not isinstance(x, str) for x in raw_include):
            raise ConfigError("include must be an array of strings")
        if len(raw_include) > MAX_INCLUDE_ENTRIES:
            raise ConfigError(f"Too many include entries (max {MAX_INCLUDE_ENTRIES})")
        for entry in raw_include:
            # Literal entries only: no ${VAR} or ~ expansion (expand() unchanged).
            target = _include_target(resolved, entry)
            visit(target, depth + 1, (*chain, resolved))
        # Unknown root keys still fail here per fragment so the error names
        # the fragment, then the final merged load re-checks the whole.
        allowed = {"data_dir", "engines", "timezone", "limits", "sandbox", "profiles",
                   "roles", "consult_role", "delegate_role", "web", "vcs", "materialize", "exclude", "selectors", "include", "insight_decisions", "workspace_cache"}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"Unknown keys in {resolved}: {', '.join(sorted(unknown))}")
        _merge_into(merged, data, resolved, origins)

    # Depth-first so shared fragments merge before the files that include
    # them; the top file merges last and any duplicate leaf names its pair.
    # Implemented by visiting includes first (above) then merging self.
    visit(top, 0, ())
    return merged


def trusted_command(data: dict, key: str, default: str) -> tuple[str, ...]:
    """Operator-owned argv array (never a shell string)."""
    command = strings(data.get(key, [default]), key)
    if not command or any(not s or "\n" in s for s in command):
        raise ConfigError(f"{key} must be a nonempty argv array")
    return command


@dataclasses.dataclass(frozen=True)
class Limits:
    # Provisional project-wide bound on newly seeded nonterminal work; 0 disables.
    open_work_items: int = 100
    # Provisional per-source bound on unresolved live producer inputs; 0 disables.
    # Congestion thresholds, not measured optima; see docs/configuration.md.
    producer_inputs_per_source: int = 20
    daily_requests: int = 0
    shared_daily_requests: int = 0
    requests_per_run: int = 24
    tools_per_run: int = 64
    #: Total wrap-up allowance granted when a per-run spend bound is hit
    #: (extra model requests / wrap-up tool calls to seal a clean `bounded`
    #: end). Project defaults; a role may override per role.
    wrap_up_requests: int = 3
    wrap_up_tools: int = 5
    # Aggregate share of the run wrap-up allowance usable by delegated
    # children; the root retains at least one request and one wrap-up tool.
    delegate_wrap_up_requests: int = 2
    delegate_wrap_up_tools: int = 4
    run_seconds: int = 1200
    model_request_seconds: int = 1200
    command_seconds: int = 300
    idle_seconds: int = 15
    cooldown_seconds: int = 5
    progress_signals: tuple[str, ...] = DEFAULT_PROGRESS_SIGNALS
    no_progress_continuations: int = 0
    no_progress_recovery_after: int = 2
    no_progress_backoff_seconds: int = 0
    default_wait_seconds: int = 1800
    maximum_wait_seconds: int = 86400
    max_failures: int = 3
    parallel_checks: int = 2
    check_pending: int = 32
    #: Active execution slots, independent of pending FIFO capacity.
    parallel_runs: int = 4
    #: Pending FIFO tickets; fixed default independent of active slots.
    admission_queue_capacity: int = 16
    parallel_consults: int = 2
    #: Operator-selected one-shot execution admission wait; 0 waits until
    #: dispatch or cancellation. Non-waiting callers still fail fast.
    admission_wait_seconds: int = 300
    #: Separate timed provider-selection recovery window; 0 disables it.
    busy_retry_seconds: int = 300
    #: Explicit drain yields persistent, non-ephemeral units at safe request boundaries.
    drain_yield: bool = True
    #: Completed Claude query/Codex turn units between safe-yield opportunities;
    #: 0 keeps cooperative preemption unavailable. Units remain adapter-specific.
    safe_point_yield_quantum: int = 0
    #: Bounded continuations after a normal engine return without a seal; full
    #: work grant only until the first tool call, then finish-only.
    missing_finish_followups: int = 1
    #: Whether exhausting those turns increments the existing failure brake.
    missing_finish_counts_as_failure: bool = True
    #: Consecutive missing-finish failures before a persistent session is
    #: rotated to a fresh conversation on its next already-admitted dispatch
    #: with unblocked open work (1-100; default 2, strictly below the default
    #: failure-brake threshold). It never schedules a run; a fully
    #: blocked/empty queue never rotates. A sealed or otherwise
    #: non-missing-finish unit resets the streak.
    missing_finish_recovery_after: int = 2
    output_bytes: int = 262144
    file_bytes: int = 8388608
    snapshot_bytes: int = 268435456
    snapshot_files: int = 20000
    history_index: int = 128
    prompt_snapshots: int = 6
    #: Maximum canonical JSON bytes of a new replacement role-owned
    #: current research state record (1 KiB-64 KiB; default 4 KiB).
    #: Existing records remain readable up to the supported 65,536-byte
    #: ceiling, even when this write bound is lowered.
    role_state_bytes: int = 4096
    #: Maximum bytes of a role's own research state injected into its
    #: prompt (1-64 KiB; default 4 KiB). Records over the bound inject
    #: status/generation/sizes only, never partial content.
    role_state_prompt_bytes: int = 4096
    free_disk_mb: int = 1024
    #: Operator-selected retention window (days) for budget day-files and
    #: decided (non-deferred) proposals. 0 disables time-based reaping;
    #: this clock never expires runs, snapshots or permanent audit records.
    retention_days: int = 31
    #: Completed, unreferenced run components; 0 retains indefinitely (0–3650).
    run_retention_days: int = 31
    #: Unreferenced snapshot/history retention; 0 disables automatic GC (0–3650).
    snapshot_retention_days: int = 31
    #: Opt-out cadence (seconds) for automatic application of the configured
    #: run/snapshot retention at safe execution boundaries; 0 disables both automatic
    #: passes while manual ``mizu prune`` and ``mizu storage`` stay available (0–2678400; default 86400).
    automatic_prune_interval_seconds: int = 86400
    #: Opt-out cadence (seconds) for automatic application of the shared
    #: smoke retention at safe execution boundaries; 0 disables automatic
    #: sweeps while manual ``mizu smoke-retention`` stays available
    #: (0–2678400; default 86400). Per-project state must not drive
    #: this global sweep; cadence lives under ``data_dir``.
    automatic_smoke_retention_interval_seconds: int = 86400
    #: Operator-selected retention (days) for per-run diagnostics and dispatch
    #: measurements (``*-events.jsonl``, ``diagnostics.txt``, and
    #: ``dispatch-attempts.json``). Files older than
    #: ``event_log_compress_days`` gzip to ``.gz``; raw and compressed logs
    #: older than ``event_log_retention_days`` are dropped. 0 disables that
    #: stage. Lifecycle records use run_retention_days, not these clocks.
    event_log_compress_days: int = 7
    event_log_retention_days: int = 31
    #: Operator-selected aggregate retention (bytes) for one run's engine
    #: event stream (``*-events.jsonl``). Bytes past this budget are dropped
    #: from the retained file with an explicit ``*-events-truncated.json``
    #: marker; parsing and the run continue, so diagnostic volume can never
    #: discard already-sealed publishable work. The per-record transport
    #: bound stays fixed mechanism, never policy.
    event_stream_bytes: int = 4 * 1024 * 1024
    #: Operator-selected bound on pending proposals offered per prompt.
    #: Raw inbox/decisions stay on disk; the operator
    #: review path (`mizu insight list`) uses a separate 1000-item bound.
    pending_insights: int = 30
    #: Absolute lifetime of an explicit insight enumeration cursor (seconds).
    insight_cursor_ttl_seconds: int = 86400
    #: Maximum open work-item rows offered in each full or delta prompt.
    work_items_prompt: int = 20
    #: Operator-selected decision-event window per dispatch (1-100;
    #: default 10). The 100-event cap stays fixed mechanism; the value is
    #: policy: with rotation, a role with many deferrals sees each one
    #: every N/value runs.
    decision_events_limit: int = 10
    #: Operator-selected terminal work-item event window per dispatch
    #: (1-100; default 10). Independent of the decision-event window above:
    #: each source uses only its own project default unless the role
    #: overrides it. The 100-event cap stays fixed mechanism.
    work_item_events_limit: int = 10
    #: Operator-selected persistent-session rotation bounds. A resumed
    #: persistent session restarts with a fresh session key once cumulative
    #: usage or wall-clock conversation lifetime reaches a bound, carrying the
    #: published snapshot and composed policy forward. 0 disables that bound;
    #: unset preserves the previous never-rotate behaviour.
    session_max_tokens: int = 0
    session_max_cost_usd: float = 0
    session_max_age_seconds: int = 0


@dataclasses.dataclass(frozen=True)
class Sandbox:
    executable: str = "podman"
    image: str = ""
    memory_mb: int = 2048
    cpus: int = 2
    pids: int = 256
    check_cache_paths: tuple[str, ...] = ()
    check_reuse: bool = False
    check_input_files: int = 250000
    check_input_bytes: int = 8589934592
    temporary_mb: int = 256
    file_mb: int = 128
    selinux_label: bool = True
    #: Container network selected by policy. `none` preserves the previous
    #: behavior. Any other value is passed to the runtime as `--network=<value>`
    #: (e.g. a managed bridge); enabling it accepts exfiltration risk, so it
    #: is an explicit operator choice, never a default. Privilege, read-only
    #: root, user mapping and seccomp floors stay fixed regardless of this knob.
    network: str = "none"
    #: Container entrypoint selected by policy (the image must provide it).
    entrypoint: str = "/bin/sh"
    #: Ordered runner arguments appended after the image and before the
    #: harness-managed `-c` script arguments. Each entry is one argv item
    #: (never shell-split); empty by default so the effective command is
    #: unchanged. Example `["-o", "pipefail"]` with `/bin/bash` keeps the
    #: option out of child shells (unlike exported `SHELLOPTS`).
    entrypoint_args: tuple[str, ...] = ()
    #: Extra read-only host mounts selected by policy. Model code can read but
    #: never write the host through these; system and harness paths are refused.
    mounts: tuple = ()
    #: Extra container environment selected by policy. Runtime vars and secret
    #: names are refused; values are recorded by key only, never by content.
    env: dict = dataclasses.field(default_factory=dict)
    mode: str = "rootless"
    namespace_helper: tuple[str, ...] = ()
    podman_root: str = ""
    podman_runroot: str = ""
    podman_tmpdir: str = ""
    storage_driver: str = ""
    storage_options: tuple[str, ...] = ()
    cgroup_manager: str = ""
    cgroup_parent: str = ""


@dataclasses.dataclass(frozen=True)
class Role:
    name: str
    profile: str
    policy: tuple
    workspace: str
    capabilities: tuple[str, ...]
    engine_tools: tuple[str, ...] = ()
    on_change: bool = False
    interval_seconds: int = 0
    calendar: tuple[str, ...] = ()
    daemon: bool = False
    selector: str = ""
    attributes: dict = dataclasses.field(default_factory=dict)
    decision_events: tuple[str, ...] = ()
    work_item_events: tuple[str, ...] = ()
    #: Independent effective event routes; load resolves legacy shared settings.
    #: "own" routes by origin submitter, "any" by subscription alone.
    decision_sources: str = "own"
    work_item_sources: str = "own"
    #: Whether a red operator-selected ``ci_branch`` wakes this role.
    #: Default true for writable roles (the writer keeps its sticky-red wake),
    #: false otherwise, so event-only/read roles never spend runs on CI they
    #: never subscribed to unless the operator opts them in.
    ci_wake: bool = False
    #: Direct exec shell state is confined to one role unit; never acceptance.
    persistent_exec: bool = False
    #: Explicit opt-in to holding this non-writer producer when project
    #: open/doing/blocked work reaches the configured cap. Per-source limits
    #: are independent and remain effective when this is false.
    held_at_capacity: bool = False
    #: Per-role per-run spend overrides. None keeps the project `[limits]`
    #: value as default; a set value must lie in the same range as the
    #: project bound. Storage,
    #: transport, budgets, slots and rotation stay project/host policy.
    requests_per_run: int | None = None
    tools_per_run: int | None = None
    #: Per-role wrap-up allowance overrides. None keeps the project `[limits]`
    #: value as default; a set value must lie in the same range as the
    #: project bound.
    wrap_up_requests: int | None = None
    wrap_up_tools: int | None = None
    #: None inherits the project wall deadline; 0 disables only this wall.
    run_seconds: int | None = None
    #: Per-role per-source event batch overrides. None inherits the matching
    #: project `[limits]` default; a set value must lie in 1-100.
    decision_events_limit: int | None = None
    #: Required explicit serialized-page budget for shared decision subscribers.
    decision_page_bytes: int | None = None
    work_item_events_limit: int | None = None
    #: Per-role no-progress timer-wake override. None inherits the project
    #: `[limits]` value; a set value must lie in 0-86400 like the project
    #: bound. Only this wake timer is per role; allowance, recovery and
    #: progress-record semantics stay project-wide.
    no_progress_backoff_seconds: int | None = None
    #: Per-role writer-daemon cooldown override. None inherits the project
    #: `[limits]` value; a set value must lie in 1-1073741824 like the
    #: project bound. Only the writable daemon cooldown and its deferred
    #: re-evaluation use it; read-only cadence stays on `idle_seconds` and
    #: read-only deferrals stay on the project `cooldown_seconds`.
    cooldown_seconds: int | None = None
    #: Per-role progress-signal override. None inherits the project
    #: `[limits]` value; a set value (including empty, which disables
    #: qualification for that role) must hold unique supported signal
    #: names like the project bound.
    progress_signals: tuple[str, ...] | None = None
    #: Per-role event-only recovery poll override. None keeps the fixed
    #: 60-second default; a set value must lie in 60-2678400 like the
    #: Task Scheduler repetition bound. Only unscheduled watched roles use
    #: it for recovery reevaluation; scheduled timers stay on their schedule.
    event_poll_seconds: int | None = None
    #: Explicit daemon scheduling policy: ``signals`` consults the
    #: role-aware ``should_run`` readiness gate, ``periodic`` runs at the
    #: ordinary cadence while the project is armed and neither paused nor
    #: draining. Load resolves an omitted value from the workspace
    #: (``write`` keeps ``signals``, ``read``/``none`` keep ``periodic``)
    #: so the daemon loop consumes this policy, never ``workspace``.
    daemon_policy: str = "periodic"
    #: Optional ordinary daemon spacing override in seconds (1-1073741824).
    #: None keeps the policy default: ``signals`` uses the effective
    #: ``cooldown_seconds``, ``periodic`` uses ``idle_seconds``. It affects
    #: ordinary daemon spacing only, never deferred re-evaluation or the
    #: ``cooldown_seconds`` retry policy.
    daemon_spacing_seconds: int | None = None


def role_requests_per_run(config: "Config", role: "Role") -> int:
    """Effective per-run provider-request bound for one role.

    Schema: role override when set, else project `[limits] requests_per_run`.
    Trust: configuration. Failure: never raises; malformed overrides read as
    the project value (load already refuses them fail-closed).
    """
    try:
        value = getattr(role, "requests_per_run", None)
    except Exception:
        value = None
    if type(value) is int and value >= 1:
        return value
    return config.limits.requests_per_run


def role_tools_per_run(config: "Config", role: "Role") -> int:
    """Effective per-run tool-call bound for one role (project default)."""
    try:
        value = getattr(role, "tools_per_run", None)
    except Exception:
        value = None
    if type(value) is int and value >= 1:
        return value
    return config.limits.tools_per_run


def role_wrap_up_requests(config: "Config", role: "Role") -> int:
    """Effective wrap-up request allowance for one role (project default)."""
    try:
        value = getattr(role, "wrap_up_requests", None)
    except Exception:
        value = None
    if type(value) is int and value >= 1:
        return value
    return config.limits.wrap_up_requests


def role_wrap_up_tools(config: "Config", role: "Role") -> int:
    """Effective wrap-up tool allowance for one role (project default)."""
    try:
        value = getattr(role, "wrap_up_tools", None)
    except Exception:
        value = None
    if type(value) is int and value >= 1:
        return value
    return config.limits.wrap_up_tools


def role_run_seconds(config: "Config", role: "Role") -> int:
    """Resolve the role's wall policy once when its Context is created."""
    return config.limits.run_seconds if role.run_seconds is None else role.run_seconds


def role_decision_events_limit(config: "Config", role: "Role") -> int:
    """Effective decision-event batch for one role for one dispatch.

    Schema: role override when set, else project `[limits]`
    decision_events_limit. Trust: configuration. Failure: never raises;
    malformed overrides read as the project value (load already refuses
    them fail-closed).
    """
    try:
        value = getattr(role, "decision_events_limit", None)
    except Exception:
        value = None
    if type(value) is int and 1 <= value <= 100:
        return value
    return config.limits.decision_events_limit


def role_work_item_events_limit(config: "Config", role: "Role") -> int:
    """Effective terminal work-item batch for one role for one dispatch.

    Schema: role override when set, else project `[limits]`
    work_item_events_limit. Trust: configuration. Failure: never raises;
    malformed overrides read as the project value (load already refuses
    them fail-closed).
    """
    try:
        value = getattr(role, "work_item_events_limit", None)
    except Exception:
        value = None
    if type(value) is int and 1 <= value <= 100:
        return value
    return config.limits.work_item_events_limit


def role_no_progress_backoff_seconds(config: "Config", role: "Role") -> int:
    """Effective no-progress timer wake for one role for one dispatch.

    Schema: role override when set, else project `[limits]`
    no_progress_backoff_seconds. Trust: configuration. Failure: never raises;
    malformed overrides read as the project value (load already refuses
    them fail-closed).
    """
    try:
        value = getattr(role, "no_progress_backoff_seconds", None)
    except Exception:
        value = None
    if type(value) is int and 0 <= value <= 86400:
        return value
    return config.limits.no_progress_backoff_seconds


def role_cooldown_seconds(config: "Config", role: "Role") -> int:
    """Effective writer-daemon cooldown for one role for one dispatch.

    Schema: role override when set, else project `[limits]`
    cooldown_seconds. Trust: configuration. Failure: never raises;
    malformed overrides read as the project value (load already refuses
    them fail-closed).
    """
    try:
        value = getattr(role, "cooldown_seconds", None)
    except Exception:
        value = None
    if type(value) is int and 1 <= value <= 1073741824:
        return value
    return config.limits.cooldown_seconds


#: Explicit daemon scheduling policies for ``roles.<name>.daemon_policy``.
DAEMON_POLICIES = frozenset({"signals", "periodic"})


def role_daemon_spacing_seconds(config: "Config", role: "Role") -> int:
    """Effective ordinary daemon spacing for one role for one dispatch.

    Schema: explicit ``daemon_spacing_seconds`` when set, else the policy
    default (``signals`` uses the effective ``cooldown_seconds``,
    ``periodic`` uses ``idle_seconds``). Trust: configuration. Failure:
    never raises; malformed overrides read as the policy default (load
    already refuses them fail-closed). Deferred re-evaluation keeps its own
    ``next_evaluation_at``/``idle_seconds``/``cooldown_seconds`` policy.
    """
    try:
        policy = getattr(role, "daemon_policy", None)
    except Exception:
        policy = None
    if policy not in DAEMON_POLICIES:
        raise ConfigError("Invalid daemon_policy; reload validated configuration")
    try:
        explicit = getattr(role, "daemon_spacing_seconds", None)
    except Exception:
        explicit = None
    if type(explicit) is int and 1 <= explicit <= 1073741824:
        return explicit
    if policy == "signals":
        return role_cooldown_seconds(config, role)
    return config.limits.idle_seconds


#: Supported progress-signal names shared by project and per-role lists.
ALLOWED_PROGRESS_SIGNALS = frozenset({"tool", "code", "state", "decision", "insight", "input"})


def role_progress_signals(config, role):
    """Effective progress-signal list for one role for one dispatch.

    Schema: role override when set (including empty, which disables
    qualification for that role), else project `[limits]`
    progress_signals. Trust: configuration. Failure: never raises;
    malformed overrides read as the project value (load already refuses
    them fail-closed).
    """
    try:
        value = getattr(role, "progress_signals", None)
    except Exception:
        value = None
    if value is None:
        try:
            return tuple(config.limits.progress_signals)
        except Exception:
            return DEFAULT_PROGRESS_SIGNALS
    if isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value):
        if len(value) == len(set(value)) and all(v in ALLOWED_PROGRESS_SIGNALS for v in value):
            return tuple(value)
    try:
        return tuple(config.limits.progress_signals)
    except Exception:
        return DEFAULT_PROGRESS_SIGNALS


def role_event_poll_seconds(role: "Role") -> int:
    """Effective event-only recovery poll for one role for one rendering.

    Schema: role override when set, else the fixed 60-second default. There
    is no project `[limits]` knob; the bounds mirror the Task Scheduler
    repetition minimum/maximum (60-2678400). Trust: configuration. Failure:
    never raises; malformed overrides read as the default (load already
    refuses them fail-closed).
    """
    try:
        value = getattr(role, "event_poll_seconds", None)
    except Exception:
        value = None
    if type(value) is int and EVENT_POLL_SECONDS_MIN <= value <= EVENT_POLL_SECONDS_MAX:
        return value
    return EVENT_POLL_SECONDS_DEFAULT


def role_policy_text(role: "Role") -> str:
    """Composed role policy text: parts read as UTF-8 and joined in order.

    Schema: tuple of policy paths on the role. Bounds: parts validated at
    config load (each at most 64 KiB, 16 parts, 256 KiB total). Trust:
    operator-owned Markdown under the config directory, never model input.
    Retry/cancellation: n/a (read at dispatch). Failure: ConfigError on
    missing/unreadable/non-UTF-8 content (fail closed, never partial text).
    """
    texts: list[str] = []
    for part in role.policy:
        try:
            raw = part.read_bytes()
        except OSError as exc:
            raise ConfigError(f"Policy file not found: {part}") from exc
        if len(raw) > MAX_ROLE_POLICY_BYTES:
            raise ConfigError(f"Policy file exceeds 64 KiB: {part}")
        try:
            texts.append(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Policy file is not UTF-8: {part}") from exc
    if not texts:
        raise ConfigError("Role policy requires at least one file")
    total = sum(len(x.encode("utf-8")) for x in texts)
    if total > MAX_ROLE_POLICY_TOTAL:
        raise ConfigError("Composed role policy exceeds 256 KiB")
    return "\n".join(piece.rstrip("\n") for piece in texts) + "\n"


def system_context_text(role: "Role", goal: str) -> str:
    """Stable engine instructions: ordered role policy, then the pinned charter.

    ``goal`` is the exact operator-supplied Context.goal, already pinned by
    the caller; never discover a charter in the managed workspace. Preserve
    its bytes (including whitespace) after the section heading. Policy reads
    retain role_policy_text's bounds and fail-closed behavior.
    """
    return role_policy_text(role) + "\n# Project charter\n\n" + goal


def role_policy_bytes(role: "Role") -> bytes:
    """Composed policy bytes for digests and engine prompts."""
    return role_policy_text(role).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class WorkspaceCache:
    roots: tuple[str, ...] = ()
    max_bytes: int = 1073741824
    check_interval_seconds: int = 3600


def workspace_cache_policy(raw: dict) -> WorkspaceCache:
    keys(raw, {"roots", "max_bytes", "check_interval_seconds"}, "workspace_cache")
    roots = strings(raw.get("roots", []), "workspace_cache.roots")
    if len(roots) > 32:
        raise ConfigError("workspace_cache.roots permits at most 32 roots")
    for root in roots:
        try:
            parts = relative_parts(root)
        except Denied as exc:
            raise ConfigError("workspace_cache.roots requires literal relative paths") from exc
        if len(root.encode("utf-8")) > 1024 or len(parts) > 32 or any(p.casefold() == ".git" for p in parts):
            raise ConfigError("Invalid workspace_cache root")
    aliases = [unicodedata.normalize("NFC", root).casefold() for root in roots]
    for i, root in enumerate(aliases):
        if any(root == other or root.startswith(other + "/") or other.startswith(root + "/")
               for other in aliases[:i]):
            raise ConfigError("workspace_cache.roots must not overlap")
    maximum = raw.get("max_bytes", 1073741824)
    interval = raw.get("check_interval_seconds", 3600)
    number(maximum, "workspace_cache.max_bytes", 1, 2**63 - 1)
    number(interval, "workspace_cache.check_interval_seconds", 1, 31 * 86400)
    return WorkspaceCache(roots, maximum, interval)


@dataclasses.dataclass(frozen=True)
class Config:
    file: Path
    data: Path
    engines: dict[str, dict]
    limits: Limits
    sandbox: Sandbox
    profiles: dict[str, dict]
    roles: dict[str, Role]
    #: Default answering role for consultation when the caller names none.
    #: Empty (unset) fails closed: consultation then requires an explicit role.
    consult_role: str
    delegate_role: str
    timezone: str
    web: dict
    vcs: dict
    materialize: dict
    exclude: tuple[str, ...]
    selectors: dict = dataclasses.field(default_factory=dict)
    tzinfo: dt.tzinfo | None = None
    insight_decisions: dict = dataclasses.field(default_factory=lambda: {
        "work_item_intent_default": "create", "work_item_intent_by_source": {}})

    #: Disabled unless the operator designates reproducible, excluded roots.
    workspace_cache: WorkspaceCache = dataclasses.field(default_factory=WorkspaceCache)

    def __post_init__(self):
        object.__setattr__(self, "tzinfo", resolve_timezone(self.timezone))

    def _raw_profile(self, profile: str) -> dict:
        try:
            return self.profiles[profile]
        except KeyError:
            raise ConfigError(f"Unknown model profile: {profile}") from None

    def engine(self, profile: str) -> str:
        raw = self._raw_profile(profile)
        engine = raw["engine"]
        if engine not in ENGINES:
            raise ConfigError(f"Unknown inference engine for profile '{profile}'")
        return engine

    def model(self, profile: str) -> dict[str, str]:
        raw = self._raw_profile(profile)
        result = {key: expand(raw[key]) for key in ("provider", "model")}
        if not result["provider"] or not result["model"]:
            raise ConfigError(f"Configure provider and model for profile '{profile}'")
        return result

    def command(self, engine: str) -> tuple[str, ...]:
        if engine not in self.engines:
            raise ConfigError(f"Unknown engine environment: {engine}")
        return self.engines[engine]["command"]

    def agent_dir(self, engine: str) -> Path:
        return self.engines[engine]["directory"]

    def options(self, profile: str) -> dict:
        return self._raw_profile(profile)["options"]


def load(file: Path) -> Config:
    file = file.expanduser().resolve()
    data = _load_merged(file)
    keys(data, {"data_dir", "engines", "timezone", "limits", "sandbox", "profiles",
                "roles", "consult_role", "delegate_role", "web", "vcs", "materialize", "exclude", "selectors", "include", "insight_decisions", "workspace_cache"}, "root")
    data.pop("include", None)
    decisions = data.get("insight_decisions", {})
    keys(decisions, {"work_item_intent_default", "work_item_intent_by_source"}, "insight_decisions")
    decisions = {"work_item_intent_default": "create", "work_item_intent_by_source": {}, **decisions}
    if decisions["work_item_intent_default"] not in ("create", "none"):
        raise ConfigError("insight_decisions.work_item_intent_default must be create or none")
    sources = decisions["work_item_intent_by_source"]
    if not isinstance(sources, dict):
        raise ConfigError("insight_decisions.work_item_intent_by_source must be a table")
    for source, intent in sources.items():
        if not ID.fullmatch(source) or intent not in ("create", "none"):
            raise ConfigError("Insight decision source policies require source identifiers and create or none")
    engines = data.get("engines", {})
    keys(engines, set(ENGINES), "engines")
    environments = {}
    for name, settings in engines.items():
        keys(settings, {"command", "directory"}, f"engines.{name}")
        environments[name] = {"command": trusted_command(settings, "command", name),
                              "directory": path_value(string(settings.get("directory", name), "directory"), file.parent)}
    lim = data.get("limits", {})
    keys(lim, {f.name for f in dataclasses.fields(Limits)}, "limits")
    # Meta-bounds on policy values so a config cannot request unbounded
    # memory/files/prompts. Defaults are TOML-overridable fallbacks.
    # daily_requests 0 disables (fail-closed); max_failures 0 disables the
    # auto-pause brake only (budgets/deadlines still bound spend and time).
    _LIMIT_RANGES = {"open_work_items": (0, 1073741824),
                     "producer_inputs_per_source": (0, 1073741824),
                     "parallel_checks": (1, 64), "check_pending": (1, 1024), "history_index": (8, 1000000), "prompt_snapshots": (1, 64),
                     "delegate_wrap_up_requests": (0, 2),
                     "delegate_wrap_up_tools": (0, 4),
                     "insight_cursor_ttl_seconds": (60, 604800),
                     "retention_days": (0, 3650), "run_retention_days": (0, 3650), "snapshot_retention_days": (0, 3650), "automatic_prune_interval_seconds": (0, 2678400), "automatic_smoke_retention_interval_seconds": (0, 2678400), "pending_insights": (1, 1000),
                     "work_items_prompt": (1, 100), "decision_events_limit": (1, 100),
                     "work_item_events_limit": (1, 100),
                     "event_log_compress_days": (0, 3650), "event_log_retention_days": (0, 3650),
                     "event_stream_bytes": (1048576, 268435456),
                     "role_state_bytes": (1024, 65536),
                     "role_state_prompt_bytes": (1, 65536),
                     "session_max_tokens": (0, 1073741824), "session_max_age_seconds": (0, 31536000),
                     "default_wait_seconds": (1, MAX_WAIT_SECONDS),
                     "maximum_wait_seconds": (1, MAX_WAIT_SECONDS),
                     "daily_requests": (0, 100000), "shared_daily_requests": (0, 100000), "max_failures": (0, 1073741824),
                     "busy_retry_seconds": (0, 86400),
                     "admission_wait_seconds": (0, 86400),
                     "no_progress_continuations": (0, 100),
                     "no_progress_recovery_after": (1, 100),
                     "no_progress_backoff_seconds": (0, 86400),
                     "missing_finish_followups": (0, 3),
                     "safe_point_yield_quantum": (0, 1000),
                     "missing_finish_recovery_after": (1, 100),
                     "free_disk_mb": (0, 1073741824)}
    allowed_progress_signals = ALLOWED_PROGRESS_SIGNALS
    for k, v in lim.items():
        if k == "progress_signals":
            values = strings(v, "limits.progress_signals")
            if len(values) != len(set(values)) or any(value not in allowed_progress_signals for value in values):
                raise ConfigError("limits.progress_signals must contain unique supported signal names")
            lim[k] = values
            continue
        if k in ("missing_finish_counts_as_failure", "drain_yield"):
            boolean(v, f"limits.{k}")
            continue
        if k == "session_max_cost_usd":
            import math
            if type(v) not in (int, float) or isinstance(v, bool) or not math.isfinite(float(v)) \
                    or not 0 <= float(v) <= 1000000:
                raise ConfigError("limits.session_max_cost_usd must be a number in [0, 1000000]")
            continue
        low, high = _LIMIT_RANGES.get(k, (1, 1073741824))
        number(v, f"limits.{k}", low, high)
    limits = Limits(**lim)
    if limits.default_wait_seconds > limits.maximum_wait_seconds:
        raise ConfigError("default wait exceeds maximum wait")
    sb = data.get("sandbox", {})
    keys(sb, {f.name for f in dataclasses.fields(Sandbox)}, "sandbox")
    for k in ("memory_mb", "cpus", "pids", "temporary_mb", "file_mb"):
        if k in sb:
            number(sb[k], f"sandbox.{k}", 1, 1048576)
    for k in ("executable", "image"):
        if k in sb:
            string(sb[k], f"sandbox.{k}")
    network = string(sb.get("network", "none"), "sandbox.network")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:=-]*", network):
        raise ConfigError("sandbox.network must be a runtime network name (default none)")
    sb["network"] = network
    entrypoint = string(sb.get("entrypoint", "/bin/sh"), "sandbox.entrypoint")
    if not entrypoint.startswith("/") or re.search(r"\s", entrypoint) or "\x00" in entrypoint:
        raise ConfigError("sandbox.entrypoint must be an absolute container path without whitespace")
    sb["entrypoint"] = entrypoint
    entrypoint_args = strings(sb.get("entrypoint_args", []), "sandbox.entrypoint_args")
    if len(entrypoint_args) > 32:
        raise ConfigError("sandbox.entrypoint_args permits at most 32 arguments")
    for arg in entrypoint_args:
        if not arg or len(arg) > 4096 or "\n" in arg:
            raise ConfigError("sandbox.entrypoint_args entries must be nonempty single-line strings of at most 4096 chars")
    sb["entrypoint_args"] = entrypoint_args
    for key in ("check_input_files", "check_input_bytes"):
        if key in sb:
            number(sb[key], "sandbox." + key, 1, 1099511627776)
    sb["check_reuse"] = boolean(sb.get("check_reuse", False), "sandbox.check_reuse")
    cache_paths = strings(sb.get("check_cache_paths", []), "sandbox.check_cache_paths")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", p) for p in cache_paths) or len(set(cache_paths)) != len(cache_paths):
        raise ConfigError("sandbox.check_cache_paths must be unique top-level artifact directories")
    sb["check_cache_paths"] = cache_paths
    raw_mounts = sb.get("mounts", [])
    if not isinstance(raw_mounts, list):
        raise ConfigError("sandbox.mounts must be an array of tables")
    mounts = []
    for index, entry in enumerate(raw_mounts):
        where = f"sandbox.mounts[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        keys(entry, {"source", "target"}, where)
        source = string(entry.get("source", ""), f"{where}.source")
        target = string(entry.get("target", ""), f"{where}.target")
        if not Path(source).is_absolute() or any(c in source for c in ("\x00", "\n", ",")):
            raise ConfigError(f"{where}.source must be an absolute host path without delimiters")
        if not target.startswith("/") or any(c in target for c in ("\x00", "\n", ",", "\\")) or ".." in PurePosixPath(target).parts:
            raise ConfigError(f"{where}.target must be an absolute POSIX path without parent references")
        normalized = "/" + "/".join(part for part in PurePosixPath(target).parts if part not in ("/", "//"))
        reserved = ("/workspace", "/work", "/tmp", "/root", "/proc", "/sys", "/dev", "/run",
                    "/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc")
        if normalized == "/" or any(normalized == p or normalized.startswith(p + "/") for p in reserved):
            raise ConfigError(f"{where}.target must not overlay system or harness paths: {target}")
        if any(normalized == m["target"] or normalized.startswith(m["target"] + "/") or
               m["target"].startswith(normalized + "/") for m in mounts):
            raise ConfigError(f"{where}.target overlaps another configured mount")
        mounts.append({"source": source, "target": normalized})
    sb["mounts"] = tuple(mounts)
    raw_env = sb.get("env", {})
    if not isinstance(raw_env, dict):
        raise ConfigError("sandbox.env must be a table")
    env = {}
    for key, value in raw_env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"sandbox.env keys must look like ENV_VAR: {key}")
        # Token-boundary match: KEY/SECRET/TOKEN as a full _-separated token.
        # MONKEY_PATH, TOKENIZERS_* and KEYCLOAK_* stay allowed; HF_TOKEN and
        # PUBLIC_KEY_PATH stay refused. Values are never inspected.
        # Process-startup hijack vars (NODE_OPTIONS/LD_PRELOAD/LD_LIBRARY_PATH/
        # PYTHONPATH/BASH_ENV) would make every node/python/sh child in the
        # container load workspace-controlled code, including the containerized
        # stdio MCP servers that share sandbox.env. The interior is always
        # Linux, so exact-name deny is sufficient; legitimate tuning belongs
        # in the image build, not per-run env. Aligned with the pi/codex
        # credential denylists and the OpenClaw MCP-env fix (GHSA-mj59-h3q9-ghfh).
        if key in ("HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX",
                   "NODE_OPTIONS", "LD_PRELOAD",
                   "LD_LIBRARY_PATH", "PYTHONPATH", "BASH_ENV") or re.search(r"(^|_)(KEY|SECRET|TOKEN)(_|$)", key):
            raise ConfigError(f"sandbox.env must not shadow runtime vars or carry secrets: {key}. "
                              "Keep credentials out of container environments.")
        if not isinstance(value, str) or "\x00" in value or "\n" in value:
            raise ConfigError(f"sandbox.env values must be single-line strings: {key}")
        env[key] = value
    sb["env"] = env
    if "selinux_label" in sb:
        boolean(sb["selinux_label"], "sandbox.selinux_label")
    if sb.get("mode", "rootless") not in ("rootless", "single"):
        raise ConfigError("sandbox.mode must be rootless or single")
    if "namespace_helper" in sb:
        sb["namespace_helper"] = strings(sb["namespace_helper"], "sandbox.namespace_helper")
        for entry in sb["namespace_helper"]:
            if not entry or "\n" in entry or "\x00" in entry:
                raise ConfigError("sandbox.namespace_helper entries must be absolute paths without newlines")
            expanded = Path(expand(entry)).expanduser()
            if not expanded.is_absolute():
                raise ConfigError("sandbox.namespace_helper entries must be absolute paths without newlines")
    else:
        sb["namespace_helper"] = ()
    for k in ("podman_root", "podman_runroot", "podman_tmpdir"):
        if k in sb and ("\x00" in sb[k] or "\n" in sb[k]):
            raise ConfigError(f"sandbox.{k} must be a string without NUL or newline")
    if sb.get("storage_driver", "") not in ("", "overlay", "vfs"):
        raise ConfigError("sandbox.storage_driver must be overlay or vfs")
    sb["storage_options"] = strings(sb.get("storage_options", []), "sandbox.storage_options")
    for opt in sb["storage_options"]:
        name, sep, _ = opt.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ConfigError("sandbox.storage_options entries must look like name=value")
    if sb.get("cgroup_manager", "") not in ("", "cgroupfs", "systemd"):
        raise ConfigError("sandbox.cgroup_manager must be cgroupfs or systemd")
    parent = string(sb.get("cgroup_parent", ""), "sandbox.cgroup_parent")
    if parent and parent.rstrip("/").rsplit("/", 1)[-1].endswith(".slice"):
        raise ConfigError("sandbox.cgroup_parent must not end in .slice (rejected by cgroupfs)")
    sandbox = Sandbox(**sb)
    if sandbox.mode == "single":
        if not sandbox.namespace_helper:
            raise ConfigError("sandbox.namespace_helper is required in single mode")
        for k in ("podman_root", "podman_runroot", "podman_tmpdir", "cgroup_parent"):
            if not getattr(sandbox, k):
                raise ConfigError(f"sandbox.{k} is required in single mode")
        if sandbox.cgroup_manager != "cgroupfs":
            raise ConfigError("sandbox.cgroup_manager must be cgroupfs in single mode")
    else:
        stale = [k for k in ("namespace_helper", "podman_root", "podman_runroot", "podman_tmpdir",
                             "storage_driver", "storage_options", "cgroup_manager", "cgroup_parent")
                 if getattr(sandbox, k)]
        if stale:
            raise ConfigError("Single-mode sandbox keys are set while mode is rootless: " + ", ".join(sorted(stale)))
    if sandbox.image and not (re.fullmatch(r"sha256:[0-9a-f]{64}", sandbox.image)
                              or re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", sandbox.image)):
        raise ConfigError("sandbox.image must be a local sha256 image ID or a digest-pinned reference")
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        raise ConfigError("At least one model profile is required")
    for name, model in profiles.items():
        if not ID.fullmatch(name):
            raise ConfigError("Invalid profile name")
        keys(model, {"provider", "model", "engine", "session", "options", "resources", "mcp_servers"}, f"profiles.{name}")
        for key in ("provider", "model", "engine", "session"):
            value = string(model.get(key, ""), f"profiles.{name}.{key}")
            if "\n" in value:
                raise ConfigError(f"Profile values must not contain newlines: {name}")
        if model.get("engine") not in environments:
            raise ConfigError(f"Configure the engine environment for {name}")
        if model.get("session") not in ("ephemeral", "persistent"):
            raise ConfigError(f"profiles.{name}.session must be ephemeral or persistent")
        for key in ("options", "mcp_servers"):
            value = model.setdefault(key, {})
            if not isinstance(value, dict):
                raise ConfigError(f"profiles.{name}.{key} must be a table")
            from .fs import canonical
            if len(canonical(value)) > 262144:
                raise ConfigError(f"profiles.{name}.{key} exceeds byte bound")
        if "mizu" in model["mcp_servers"]:
            raise ConfigError("mcp_servers.mizu is owned by the runtime")
        if len(model['mcp_servers']) > 64:
            raise ConfigError('mcp_servers accepts at most 64 explicit servers')
        resources = model.setdefault("resources", [])
        if not isinstance(resources, list) or len(resources) > MAX_RESOURCES:
            raise ConfigError(f"resources must be an array of at most {MAX_RESOURCES} entries")
        seen = set()
        for entry in resources:
            keys(entry, {"kind", "path", "sha256"}, "resource")
            string(entry.get("kind", ""), "resource.kind")
            raw_path = string(entry.get("path", ""), "resource.path")
            joined = Path(os.path.expanduser(expand(raw_path)))
            joined = joined if joined.is_absolute() else file.parent / joined
            if joined.is_symlink() or not (joined.is_file() or joined.is_dir()):
                raise ConfigError("Resource must be local content without symlinks")
            entry["path"] = str(joined.resolve())
            sha = string(entry.get("sha256", ""), "resource.sha256")
            if not re.fullmatch(r"[0-9a-f]{64}", sha) or entry["path"] in seen:
                raise ConfigError("Resource requires a unique path and SHA-256 digest")
            seen.add(entry["path"])
    from .selection import validate_selectors, attributes
    selectors = validate_selectors(data.get("selectors", {}), profiles, file.parent)
    roles: dict[str, Role] = {}
    role_tables = data.get("roles", {})
    if not isinstance(role_tables, dict):
        raise ConfigError("roles must be a table")
    for name, role in role_tables.items():
        if not ID.fullmatch(name):
            raise ConfigError("Invalid role name")
        if name in RESERVED_ROLE_NAMES:
            raise ConfigError(f"Role name '{name}' is reserved for the operator channel")
        keys(role, {"profile", "policy", "workspace", "capabilities", "engine_tools", "on_change",
                    "interval_seconds", "calendar", "daemon", "selector", "attributes",
                    "decision_events", "work_item_events", "decision_sources", "work_item_sources", "ci_wake", "held_at_capacity", "persistent_exec", "requests_per_run", "tools_per_run", "wrap_up_requests", "wrap_up_tools", "run_seconds", "decision_events_limit", "decision_page_bytes", "work_item_events_limit", "no_progress_backoff_seconds", "cooldown_seconds", "progress_signals", "event_poll_seconds", "daemon_policy", "daemon_spacing_seconds"}, f"roles.{name}")
        profile = string(role.get("profile", ""), f"roles.{name}.profile")
        selector = string(role.get("selector", ""), f"roles.{name}.selector")
        if ("profile" in role) == ("selector" in role):
            raise ConfigError(f"Role {name} requires exactly one of profile or selector")
        if selector and selector not in selectors or not selector and profile not in profiles:
            raise ConfigError(f"Unknown profile or selector for role {name}")
        attrs = attributes(role.get("attributes", {}))
        workspace = role.get("workspace", "read")
        if workspace not in ("write", "read", "none"):
            raise ConfigError("workspace must be write, read, or none")
        caps = strings(role.get("capabilities", []), "capabilities")
        if set(caps) - CAPABILITIES or len(set(caps)) != len(caps):
            raise ConfigError(f"Invalid or duplicate capability in role {name}")
        if "withdraw_decided" in caps and "withdraw_insight" not in caps:
            raise ConfigError(f"Role {name}: withdraw_decided requires withdraw_insight")
        engine_tools = strings(role.get("engine_tools", []), "engine_tools")
        if len(engine_tools) > 128 or len(set(engine_tools)) != len(engine_tools) or any(not tool or len(tool) > 256 for tool in engine_tools):
            raise ConfigError("engine_tools must contain at most 128 unique bounded names")
        if set(engine_tools) & {"bash", "powershell", "edit", "write", "read", "Bash", "Read", "Edit", "Write", "NotebookEdit", "Computer", "Glob", "Grep"}:
            raise ConfigError("Native host tools bypass the OCI bridge; grant mizu operations instead")
        if "finish" not in caps:
            raise ConfigError(f"Role {name} requires the finish capability")
        if "verify" in caps and workspace != "write":
            raise ConfigError("verify requires a writable workspace")
        if "sync" in caps and workspace != "write":
            raise ConfigError("sync requires a writable workspace")
        if "vcs_publish" in caps and workspace != "write":
            raise ConfigError("vcs_publish requires a writable workspace")
        if "vcs_publish" in caps and "decide" in caps:
            raise ConfigError(f"Role {name} must not combine vcs_publish with decide; "
                              "it may submit ordinary insights but must never approve "
                              "its own mutation (use a dedicated publisher role without self-approval)")
        if "vcs_retire" in caps and workspace == "none":
            raise ConfigError("vcs_retire requires a visible workspace")
        if "vcs_retire" in caps and "decide" in caps:
            raise ConfigError(f"Role {name} must not combine vcs_retire with decide; "
                              "it may submit ordinary insights but must never approve "
                              "its own mutation (use a dedicated integrator role without self-approval)")
        if "vcs_dispose" in caps and workspace == "none":
            raise ConfigError("vcs_dispose requires a visible workspace")
        if "vcs_dispose" in caps and "decide" in caps:
            raise ConfigError(f"Role {name} must not combine vcs_dispose with decide; "
                              "it may submit ordinary insights but must never approve "
                              "its own mutation (use a dedicated integrator role without self-approval)")
        if workspace == "write" and "verify" not in caps:
            raise ConfigError(f"Writable role {name} requires the verify capability so "
                              "completion can be bound to acceptance commands")
        raw_policy = role.get("policy", "")
        if isinstance(raw_policy, str):
            entries = [raw_policy]
        elif isinstance(raw_policy, list) and all(isinstance(x, str) for x in raw_policy):
            entries = list(raw_policy)
        else:
            raise ConfigError(f"roles.{name}.policy must be a path string or an array of path strings")
        if not entries or len(entries) > MAX_ROLE_POLICY_PARTS:
            raise ConfigError(f"roles.{name}.policy must list 1-{MAX_ROLE_POLICY_PARTS} files")
        if len(set(entries)) != len(entries):
            raise ConfigError(f"roles.{name}.policy lists a duplicate file")
        policies: list[Path] = []
        for entry in entries:
            candidate = path_value(string(entry, "policy"), file.parent)
            if not candidate.is_file():
                raise ConfigError(f"Policy file not found: {candidate}")
            try:
                size = candidate.stat().st_size
            except OSError as exc:
                raise ConfigError(f"Policy file not found: {candidate}") from exc
            if size > MAX_ROLE_POLICY_BYTES:
                raise ConfigError(f"Policy file exceeds 64 KiB: {candidate}")
            policies.append(candidate)
        try:
            total = sum(len(part.read_bytes()) for part in policies)
        except OSError as exc:
            raise ConfigError(f"Policy file not found: {exc}") from exc
        if total > MAX_ROLE_POLICY_TOTAL:
            raise ConfigError(f"Composed role policy exceeds 256 KiB: {name}")
        policy = tuple(policies)
        interval = number(role.get("interval_seconds", 0), "interval_seconds", 0, 31536000)
        calendar = strings(role.get("calendar", []), "calendar")
        if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in calendar):
            raise ConfigError("calendar entries must be HH:MM")
        daemon = boolean(role.get("daemon", False), "daemon")
        if sum((bool(interval), bool(calendar), daemon)) > 1:
            raise ConfigError("Choose one scheduling method per role")
        raw_events = role.get("decision_events", [])
        events = strings(raw_events, f"roles.{name}.decision_events")
        if len(events) > 8 or len(set(events)) != len(events):
            raise ConfigError(f"roles.{name}.decision_events must list at most 8 unique actions")
        for event in events:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", event):
                raise ConfigError(f"roles.{name}.decision_events entries must be lowercase action names")
            if event == "withdraw":
                raise ConfigError(f"roles.{name}.decision_events must never include withdraw; "
                                  "withdrawn findings are never reanimated")
        work_events = strings(role.get("work_item_events", []), f"roles.{name}.work_item_events")
        if len(set(work_events)) != len(work_events) or any(e not in ("done", "dropped") for e in work_events):
            raise ConfigError(f"roles.{name}.work_item_events must list unique done/dropped values")
        raw_sources = role.get("decision_sources", "own")
        work_sources = role.get("work_item_sources", raw_sources if work_events else "own")
        for key, value in (("decision_sources", raw_sources), ("work_item_sources", work_sources)):
            if not isinstance(value, str) or value not in ("own", "any"):
                raise ConfigError(f"roles.{name}.{key} must be own or any")
        if "work_item_sources" not in role:
            # Legacy shared route: keep terminal delivery, but do not invent
            # an any-source decision subscription for a terminal-only role.
            if raw_sources == "any" and not (events or work_events):
                raise ConfigError(f"roles.{name}.decision_sources is any without decision_events or work_item_events; "
                                  "subscribe to at least one action")
            if not events:
                raw_sources = "own"
        else:
            for key, value, subscription in (("decision_sources", raw_sources, events),
                                             ("work_item_sources", work_sources, work_events)):
                if value == "any" and not subscription:
                    raise ConfigError(f"roles.{name}.{key} is any without its event subscription")
        ci_wake = boolean(role.get("ci_wake", workspace == "write"), f"roles.{name}.ci_wake")
        held_at_capacity = boolean(role.get("held_at_capacity", False),
                                   f"roles.{name}.held_at_capacity")
        persistent_exec = boolean(role.get("persistent_exec", False), f"roles.{name}.persistent_exec")
        if persistent_exec and "exec" not in caps:
            raise ConfigError(f"roles.{name}.persistent_exec requires the exec capability")
        role_requests = role.get("requests_per_run")
        if role_requests is not None:
            number(role_requests, f"roles.{name}.requests_per_run", 1, 1073741824)
        role_tools = role.get("tools_per_run")
        if role_tools is not None:
            number(role_tools, f"roles.{name}.tools_per_run", 1, 1073741824)
        role_wrap_requests = role.get("wrap_up_requests")
        if role_wrap_requests is not None:
            number(role_wrap_requests, f"roles.{name}.wrap_up_requests", 1, 1073741824)
        role_wrap_tools = role.get("wrap_up_tools")
        if role_wrap_tools is not None:
            number(role_wrap_tools, f"roles.{name}.wrap_up_tools", 1, 1073741824)
        role_seconds = role.get("run_seconds")
        if role_seconds is not None:
            number(role_seconds, f"roles.{name}.run_seconds", 0, 1073741824)
        role_decision_bytes = role.get("decision_page_bytes")
        if role_decision_bytes is not None:
            number(role_decision_bytes, f"roles.{name}.decision_page_bytes", 1, 1048576)
        role_decision_limit = role.get("decision_events_limit")
        if role_decision_limit is not None:
            number(role_decision_limit, f"roles.{name}.decision_events_limit", 1, 100)
        role_work_item_limit = role.get("work_item_events_limit")
        if role_work_item_limit is not None:
            number(role_work_item_limit, f"roles.{name}.work_item_events_limit", 1, 100)
        role_backoff = role.get("no_progress_backoff_seconds")
        if role_backoff is not None:
            number(role_backoff, f"roles.{name}.no_progress_backoff_seconds", 0, 86400)
        role_cooldown = role.get("cooldown_seconds")
        if role_cooldown is not None:
            number(role_cooldown, f"roles.{name}.cooldown_seconds", 1, 1073741824)
        role_progress = role.get("progress_signals")
        if role_progress is not None:
            values = strings(role_progress, f"roles.{name}.progress_signals")
            if len(values) != len(set(values)) or any(value not in ALLOWED_PROGRESS_SIGNALS for value in values):
                raise ConfigError(f"roles.{name}.progress_signals must contain unique supported signal names")
            role_progress = values
        else:
            role_progress = None
        role_event_poll = role.get("event_poll_seconds")
        if role_event_poll is not None:
            number(role_event_poll, f"roles.{name}.event_poll_seconds",
                   EVENT_POLL_SECONDS_MIN, EVENT_POLL_SECONDS_MAX)
        raw_daemon_policy = role.get("daemon_policy")
        if raw_daemon_policy is None:
            daemon_policy = "signals" if workspace == "write" else "periodic"
        else:
            if type(raw_daemon_policy) is not str or raw_daemon_policy not in ("signals", "periodic"):
                raise ConfigError(f"roles.{name}.daemon_policy must be signals or periodic")
            daemon_policy = raw_daemon_policy
        role_daemon_spacing = role.get("daemon_spacing_seconds")
        if role_daemon_spacing is not None:
            number(role_daemon_spacing, f"roles.{name}.daemon_spacing_seconds", 1, 1073741824)
        roles[name] = Role(name, profile, policy, workspace, caps,
                           strings(role.get("engine_tools", []), "engine_tools"),
                           boolean(role.get("on_change", False), "on_change"), interval, calendar, daemon, selector, attrs,
                           decision_events=events, work_item_events=work_events, decision_sources=raw_sources,
                           work_item_sources=work_sources, ci_wake=ci_wake,
                           held_at_capacity=held_at_capacity, persistent_exec=persistent_exec,
                           requests_per_run=role_requests, tools_per_run=role_tools,
                           wrap_up_requests=role_wrap_requests, wrap_up_tools=role_wrap_tools,
                           run_seconds=role_seconds,
                           decision_events_limit=role_decision_limit, decision_page_bytes=role_decision_bytes,
                           work_item_events_limit=role_work_item_limit,
                           no_progress_backoff_seconds=role_backoff,
                           cooldown_seconds=role_cooldown,
                           progress_signals=role_progress,
                           event_poll_seconds=role_event_poll,
                           daemon_policy=daemon_policy,
                           daemon_spacing_seconds=role_daemon_spacing)
    if not roles:
        raise ConfigError("At least one role is required")
    consult_role = string(data.get("consult_role", ""), "consult_role")
    if consult_role:
        if not ID.fullmatch(consult_role):
            raise ConfigError("Invalid consult_role name")
        if consult_role not in roles:
            raise ConfigError(f"consult_role is not a configured role: {consult_role}")
    delegate_role = string(data.get("delegate_role", ""), "delegate_role")
    if delegate_role:
        if not ID.fullmatch(delegate_role):
            raise ConfigError("Invalid delegate_role name")
        if delegate_role not in roles:
            raise ConfigError(f"delegate_role is not a configured role: {delegate_role}")
    timezone = string(data.get("timezone", "UTC"), "timezone")
    web = data.get("web", {})
    keys(web, {"hosts", "feeds", "cache_seconds", "timeout_seconds", "max_bytes", "diagnostic_bytes",
               "search_command", "destination_command", "intranet", "probe_hosts", "probe_methods"}, "web")
    web = {"hosts": [], "feeds": [], "cache_seconds": 1800, "timeout_seconds": 20,
           "max_bytes": 524288, "diagnostic_bytes": ADAPTER_DIAGNOSTIC_BYTES_DEFAULT,
           "search_command": [], "destination_command": [], "intranet": False,
           "probe_hosts": [], "probe_methods": [], **web}
    for k in ("hosts", "feeds", "search_command", "destination_command", "probe_hosts", "probe_methods"):
        strings(web[k], f"web.{k}")
    if web["destination_command"] and any(not s or "\n" in s for s in web["destination_command"]):
        raise ConfigError("web.destination_command must be a nonempty argv array without newlines")
    import re as _re_probe
    for _m in web["probe_methods"]:
        if not _re_probe.fullmatch(r"[A-Z]{1,16}", _m):
            raise ConfigError("web.probe_methods entries must be uppercase HTTP tokens (1-16 chars)")
    if len(set(web["probe_methods"])) != len(web["probe_methods"]):
        raise ConfigError("web.probe_methods must not contain duplicates")
    for k in ("cache_seconds", "timeout_seconds", "max_bytes"):
        number(web[k], f"web.{k}", 1, 16777216)
    number(web["diagnostic_bytes"], "web.diagnostic_bytes", 0, ADAPTER_DIAGNOSTIC_BYTES_MAX)
    web["intranet"] = boolean(web["intranet"], "web.intranet")
    vcs = data.get("vcs", {})
    keys(vcs, {"command", "timeout_seconds", "max_bytes", "diagnostic_bytes", "ci_branch", "ci_wake_extra_branches", "ci_rewake_seconds",
               "poll_enabled", "poll_proposals_enabled",
               "poll_interval_seconds", "poll_max_branches",
               "poll_fetch_timeout_seconds", "poll_status_timeout_seconds",
               "retire_grant", "owned_prefixes", "protected_refs",
               "close_grant", "merge_grant", "request_grant"}, "vcs")
    vcs = {"command": [], "timeout_seconds": 20,
           "max_bytes": 524288, "diagnostic_bytes": ADAPTER_DIAGNOSTIC_BYTES_DEFAULT,
           "ci_branch": "", "ci_wake_extra_branches": [], "ci_rewake_seconds": 1800,
           "poll_enabled": False, "poll_proposals_enabled": False,
           "poll_interval_seconds": 300, "poll_max_branches": 4,
           "poll_fetch_timeout_seconds": 30,
           "poll_status_timeout_seconds": 15, "retire_grant": False,
           "owned_prefixes": [], "protected_refs": [],
           "close_grant": False, "merge_grant": False, "request_grant": False, **vcs}
    strings(vcs["command"], "vcs.command")
    if vcs["command"] and any(not s or "\n" in s for s in vcs["command"]):
        raise ConfigError("vcs.command must be a nonempty argv array without newlines")
    for k in ("timeout_seconds", "max_bytes"):
        number(vcs[k], f"vcs.{k}", 1, 16777216)
    number(vcs["diagnostic_bytes"], "vcs.diagnostic_bytes", 0, ADAPTER_DIAGNOSTIC_BYTES_MAX)
    vcs["ci_branch"] = string(vcs["ci_branch"], "vcs.ci_branch")
    if vcs["ci_branch"] and (len(vcs["ci_branch"]) > 256 or any(c.isspace() for c in vcs["ci_branch"])):
        raise ConfigError("vcs.ci_branch must be empty or a branch name without whitespace (at most 256 chars)")
    vcs["ci_wake_extra_branches"] = strings(
        vcs["ci_wake_extra_branches"], "vcs.ci_wake_extra_branches")
    if len(vcs["ci_wake_extra_branches"]) > MAX_CI_WAKE_EXTRA_BRANCHES:
        raise ConfigError("vcs.ci_wake_extra_branches must contain at most 64 branch names")
    from .vcs import check_branch as _check_vcs_branch
    for branch in vcs["ci_wake_extra_branches"]:
        try:
            _check_vcs_branch(branch)
            if any(character.isspace() for character in branch):
                raise ValueError("whitespace in branch name")
        except Exception as exc:
            raise ConfigError(
                "vcs.ci_wake_extra_branches must contain valid branch names") from exc
    vcs["retire_grant"] = boolean(vcs["retire_grant"], "vcs.retire_grant")
    vcs["close_grant"] = boolean(vcs["close_grant"], "vcs.close_grant")
    vcs["merge_grant"] = boolean(vcs["merge_grant"], "vcs.merge_grant")
    vcs["request_grant"] = boolean(vcs["request_grant"], "vcs.request_grant")
    vcs["owned_prefixes"] = strings(vcs["owned_prefixes"], "vcs.owned_prefixes")
    for prefix in vcs["owned_prefixes"]:
        _vcs_prefix(prefix, "vcs.owned_prefixes")
    vcs["protected_refs"] = strings(vcs["protected_refs"], "vcs.protected_refs")
    for ref in vcs["protected_refs"]:
        if len(ref) > 256 or not ref or any(c.isspace() for c in ref):
            raise ConfigError("vcs.protected_refs must hold branch names without whitespace (at most 256 chars)")
    number(vcs["ci_rewake_seconds"], "vcs.ci_rewake_seconds", 60, 86400)
    vcs["poll_enabled"] = boolean(vcs["poll_enabled"], "vcs.poll_enabled")
    vcs["poll_proposals_enabled"] = boolean(vcs["poll_proposals_enabled"], "vcs.poll_proposals_enabled")
    number(vcs["poll_interval_seconds"], "vcs.poll_interval_seconds", 15, 86400)
    number(vcs["poll_max_branches"], "vcs.poll_max_branches", 1, 64)
    number(vcs["poll_fetch_timeout_seconds"], "vcs.poll_fetch_timeout_seconds", 1, 300)
    number(vcs["poll_status_timeout_seconds"], "vcs.poll_status_timeout_seconds", 1, 300)
    mat = data.get("materialize", {})
    keys(mat, {"command", "timeout_seconds", "max_bytes", "diagnostic_bytes"},
         "materialize")
    mat = {"command": [], "timeout_seconds": 20, "max_bytes": 524288,
           "diagnostic_bytes": ADAPTER_DIAGNOSTIC_BYTES_DEFAULT, **mat}
    strings(mat["command"], "materialize.command")
    if mat["command"] and any(not s or "\n" in s for s in mat["command"]):
        raise ConfigError("materialize.command must be a nonempty argv array without newlines")
    for k in ("timeout_seconds", "max_bytes"):
        number(mat[k], f"materialize.{k}", 1, 16777216)
    number(mat["diagnostic_bytes"], "materialize.diagnostic_bytes", 0, ADAPTER_DIAGNOSTIC_BYTES_MAX)
    return Config(file, path_value(string(data.get("data_dir", "~/.local/state/mizu"), "data_dir"), file.parent),
                  environments, limits, sandbox, profiles, roles, consult_role, delegate_role, timezone, web, vcs, mat,
                  strings(data.get("exclude", [".git", ".pi", ".env", ".env.*", ".venv",
                                                "node_modules", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache"]), "exclude"), selectors, insight_decisions=decisions,
                  workspace_cache=workspace_cache_policy(data.get("workspace_cache", {})))
