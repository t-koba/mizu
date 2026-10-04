"""Strict TOML configuration. Mechanisms interpret grants, not role prose."""
from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError
from .fs import ID
CAPABILITIES = frozenset({"diff", "files", "read", "exec", "experiment", "verify", "fetch",
                          "search", "insights", "decide", "submit_insight", "consult",
                          "report", "finish", "sync", "vcs_read", "vcs_publish"})
ENGINES = ("pi", "codex", "claude")
#: Role names that collide with insight sources owned by the host/operator
#: channel (operator CLI, vcs CI helper, editor outbox). Model
#: `submit_insight` fixes source to the role name, so these are refused
#: at config load to keep the operator channel unforgeable.
RESERVED_ROLE_NAMES = frozenset({"operator", "vcs", "editor"})
MAX_RESOURCES = 64
#: Protocol wait bound mirrored in protocol.finish.wait_seconds; keep both at 86400.
MAX_WAIT_SECONDS = 86400
#: Shared-fragment `include` bounds (fixed mechanism, not knobs per ADR-006).
MAX_INCLUDE_DEPTH = 8
MAX_INCLUDE_FILES = 32
MAX_INCLUDE_BYTES = 1048576
MAX_INCLUDE_ENTRIES = 32


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
    if candidate.is_absolute():
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
                   "roles", "consult_profiles", "web", "vcs", "exclude", "selectors", "include"}
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
    daily_requests: int = 0
    shared_daily_requests: int = 0
    requests_per_run: int = 24
    tools_per_run: int = 64
    run_seconds: int = 1200
    command_seconds: int = 300
    idle_seconds: int = 15
    cooldown_seconds: int = 5
    default_wait_seconds: int = 1800
    maximum_wait_seconds: int = 86400
    max_failures: int = 3
    parallel_runs: int = 4
    parallel_consults: int = 2
    output_bytes: int = 262144
    file_bytes: int = 8388608
    snapshot_bytes: int = 268435456
    snapshot_files: int = 20000
    history_index: int = 128
    prompt_snapshots: int = 6
    free_disk_mb: int = 1024
    #: Operator-selected retention window (days) for budget day-files and
    #: decided (non-deferred) proposals. 0 disables time-based reaping;
    #: snapshots, runs, decisions and evidence are never reaped.
    retention_days: int = 31
    #: Operator-selected bound on pending proposals offered per prompt /
    #: dashboard projection. Raw inbox/decisions stay on disk; the operator
    #: review path (`mizu insight list`) uses a separate 1000-item bound.
    pending_insights: int = 30


@dataclasses.dataclass(frozen=True)
class Sandbox:
    executable: str = "podman"
    image: str = ""
    memory_mb: int = 2048
    cpus: int = 2
    pids: int = 256
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
    policy: Path
    workspace: str
    capabilities: tuple[str, ...]
    engine_tools: tuple[str, ...] = ()
    on_change: bool = False
    interval_seconds: int = 0
    calendar: tuple[str, ...] = ()
    daemon: bool = False
    selector: str = ""
    attributes: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Config:
    file: Path
    data: Path
    engines: dict[str, dict]
    limits: Limits
    sandbox: Sandbox
    profiles: dict[str, dict]
    roles: dict[str, Role]
    consult_profiles: tuple[str, ...]
    timezone: str
    web: dict
    vcs: dict
    exclude: tuple[str, ...]
    selectors: dict = dataclasses.field(default_factory=dict)

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
                "roles", "consult_profiles", "web", "vcs", "exclude", "selectors", "include"}, "root")
    data.pop("include", None)
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
    _LIMIT_RANGES = {"history_index": (8, 1000000), "prompt_snapshots": (1, 64),
                     "retention_days": (0, 3650), "pending_insights": (1, 1000),
                     "default_wait_seconds": (1, MAX_WAIT_SECONDS),
                     "maximum_wait_seconds": (1, MAX_WAIT_SECONDS),
                     "daily_requests": (0, 100000), "shared_daily_requests": (0, 100000), "max_failures": (0, 1073741824),
                     "free_disk_mb": (0, 1073741824)}
    for k, v in lim.items():
        low, high = _LIMIT_RANGES.get(k, (1, 1073741824))
        number(v, f"limits.{k}", low, high)
    limits = Limits(**lim)
    if limits.default_wait_seconds > limits.maximum_wait_seconds:
        raise ConfigError("default wait exceeds maximum wait")
    if limits.parallel_consults > limits.parallel_runs:
        raise ConfigError("parallel_consults must not exceed parallel_runs")
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
        if key in ("HOME", "TMPDIR", "PATH") or re.search(r"(^|_)(KEY|SECRET|TOKEN)(_|$)", key):
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
                    "interval_seconds", "calendar", "daemon", "selector", "attributes"}, f"roles.{name}")
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
        if "vcs_publish" in caps and ("submit_insight" in caps or "decide" in caps):
            raise ConfigError(f"Role {name} must not combine vcs_publish with submit_insight/decide; "
                              "use a dedicated publisher role without self-approval")
        if workspace == "write" and "verify" not in caps:
            raise ConfigError(f"Writable role {name} requires the verify capability so "
                              "completion can be bound to acceptance commands")
        policy = path_value(string(role.get("policy", ""), "policy"), file.parent)
        if not policy.is_file():
            raise ConfigError(f"Policy file not found: {policy}")
        interval = number(role.get("interval_seconds", 0), "interval_seconds", 0, 31536000)
        calendar = strings(role.get("calendar", []), "calendar")
        if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in calendar):
            raise ConfigError("calendar entries must be HH:MM")
        daemon = boolean(role.get("daemon", False), "daemon")
        if sum((bool(interval), bool(calendar), daemon)) > 1:
            raise ConfigError("Choose one scheduling method per role")
        roles[name] = Role(name, profile, policy, workspace, caps,
                           strings(role.get("engine_tools", []), "engine_tools"),
                           boolean(role.get("on_change", False), "on_change"), interval, calendar, daemon, selector, attrs)
    if not roles:
        raise ConfigError("At least one role is required")
    consult = strings(data.get("consult_profiles", []), "consult_profiles")
    if any(p not in profiles for p in consult) or len(set(consult)) != len(consult):
        raise ConfigError("consult_profiles must contain unique, configured profiles")
    timezone = string(data.get("timezone", "UTC"), "timezone")
    resolve_timezone(timezone)
    web = data.get("web", {})
    keys(web, {"hosts", "feeds", "cache_seconds", "timeout_seconds", "max_bytes", "search_command",
               "intranet"}, "web")
    web = {"hosts": [], "feeds": [], "cache_seconds": 1800, "timeout_seconds": 20,
           "max_bytes": 524288, "search_command": [], "intranet": False, **web}
    for k in ("hosts", "feeds", "search_command"):
        strings(web[k], f"web.{k}")
    for k in ("cache_seconds", "timeout_seconds", "max_bytes"):
        number(web[k], f"web.{k}", 1, 16777216)
    web["intranet"] = boolean(web["intranet"], "web.intranet")
    vcs = data.get("vcs", {})
    keys(vcs, {"command", "timeout_seconds", "max_bytes", "poll_enabled",
               "poll_interval_seconds"}, "vcs")
    vcs = {"command": [], "timeout_seconds": 20,
           "max_bytes": 524288, "poll_enabled": False,
           "poll_interval_seconds": 300, **vcs}
    strings(vcs["command"], "vcs.command")
    if vcs["command"] and any(not s or "\n" in s for s in vcs["command"]):
        raise ConfigError("vcs.command must be a nonempty argv array without newlines")
    for k in ("timeout_seconds", "max_bytes"):
        number(vcs[k], f"vcs.{k}", 1, 16777216)
    vcs["poll_enabled"] = boolean(vcs["poll_enabled"], "vcs.poll_enabled")
    number(vcs["poll_interval_seconds"], "vcs.poll_interval_seconds", 15, 86400)
    return Config(file, path_value(string(data.get("data_dir", "~/.local/state/mizu"), "data_dir"), file.parent),
                  environments, limits, sandbox, profiles, roles, consult, timezone, web, vcs,
                  strings(data.get("exclude", [".git", ".pi", ".env", ".env.*", ".venv",
                                                "node_modules", "__pycache__", ".pytest_cache"]), "exclude"), selectors)
