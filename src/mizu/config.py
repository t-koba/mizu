"""Strict TOML configuration. Mechanisms interpret grants, not role prose."""
from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError
from .fs import ID

CAPABILITIES = frozenset({"diff", "files", "read", "exec", "experiment", "verify", "fetch",
                          "search", "insights", "decide", "submit_insight", "consult",
                          "report", "finish"})
#: Inference engines selectable per profile. `pi` is the default and preserves
#: existing behavior; `codex`/`claude` route through the generic driver
#: registry with the same work-unit contract (see ADR-007).
ENGINES = ("pi", "codex", "claude")
#: Accepted Pi thinking levels for the pinned interface (see adapters/pi/compatibility.json).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
#: Protocol wait bound mirrored in protocol.finish.wait_seconds; keep both at 86400.
MAX_WAIT_SECONDS = 86400


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


@dataclasses.dataclass(frozen=True)
class Limits:
    daily_requests: int = 0
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
    persistent: bool = False
    on_change: bool = False
    interval_seconds: int = 0
    calendar: tuple[str, ...] = ()
    daemon: bool = False


@dataclasses.dataclass(frozen=True)
class Config:
    file: Path
    data: Path
    pi_command: tuple[str, ...]
    codex_command: tuple[str, ...]
    claude_command: tuple[str, ...]
    pi_dir: Path
    limits: Limits
    sandbox: Sandbox
    profiles: dict[str, dict]
    roles: dict[str, Role]
    consult_profiles: tuple[str, ...]
    timezone: str
    web: dict
    exclude: tuple[str, ...]

    def engine(self, profile: str) -> str:
        try:
            raw = self.profiles[profile]
        except KeyError:
            raise ConfigError(f"Unknown model profile: {profile}") from None
        engine = raw.get("engine", "pi")
        if engine not in ENGINES:
            raise ConfigError(f"Unknown inference engine for profile '{profile}'")
        return engine

    def model(self, profile: str) -> dict[str, str]:
        try:
            raw = self.profiles[profile]
        except KeyError:
            raise ConfigError(f"Unknown model profile: {profile}") from None
        result = {key: expand(raw.get(key, "")) for key in ("provider", "model", "thinking")}
        if not result["provider"] or not result["model"]:
            raise ConfigError(f"Configure provider and model for profile '{profile}'")
        return result


def load(file: Path) -> Config:
    file = file.expanduser().resolve()
    try:
        data = tomllib.loads(file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load configuration: {exc}") from exc
    keys(data, {"schema", "data_dir", "pi_command", "codex_command", "claude_command",
                "pi_dir", "timezone", "limits",
                "sandbox", "profiles", "roles", "consult_profiles", "web", "exclude"}, "root")
    if data.get("schema") != 1:
        raise ConfigError("Only configuration schema = 1 is supported")
    lim = data.get("limits", {})
    keys(lim, {f.name for f in dataclasses.fields(Limits)}, "limits")
    for k, v in lim.items():
        if k in ("history_index",):
            number(v, f"limits.{k}", 8, 1000000)
        elif k in ("prompt_snapshots",):
            number(v, f"limits.{k}", 1, 64)
        elif k in ("default_wait_seconds", "maximum_wait_seconds"):
            number(v, f"limits.{k}", 1, MAX_WAIT_SECONDS)
        elif k in ("daily_requests",):
            # One admission ≈ one paid provider call; the bound keeps the
            # day-file (count and bytes) proportional to real usage.
            number(v, f"limits.{k}", 0, 100000)
        else:
            number(v, f"limits.{k}", 0 if k in ("daily_requests", "free_disk_mb") else 1,
                   1073741824)
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
        keys(model, {"provider", "model", "thinking", "engine"}, f"profiles.{name}")
        for k, v in model.items():
            string(v, f"profiles.{name}.{k}")
        if any("\n" in v for v in model.values()):
            raise ConfigError(f"Profile values must not contain newlines: {name}")
        if model.get("thinking", "off") not in THINKING_LEVELS:
            raise ConfigError(f"Invalid thinking level for {name}")
        if model.get("engine", "pi") not in ENGINES:
            raise ConfigError(f"Unknown inference engine for {name}")
    roles: dict[str, Role] = {}
    role_tables = data.get("roles", {})
    if not isinstance(role_tables, dict):
        raise ConfigError("roles must be a table")
    for name, role in role_tables.items():
        if not ID.fullmatch(name):
            raise ConfigError("Invalid role name")
        keys(role, {"profile", "policy", "workspace", "capabilities", "persistent", "on_change",
                    "interval_seconds", "calendar", "daemon"}, f"roles.{name}")
        profile = string(role.get("profile", ""), f"roles.{name}.profile")
        if profile not in profiles:
            raise ConfigError(f"Unknown profile for role {name}")
        workspace = role.get("workspace", "read")
        if workspace not in ("write", "read", "none"):
            raise ConfigError("workspace must be write, read, or none")
        caps = strings(role.get("capabilities", []), "capabilities")
        if set(caps) - CAPABILITIES or len(set(caps)) != len(caps):
            raise ConfigError(f"Invalid or duplicate capability in role {name}")
        if "finish" not in caps:
            raise ConfigError(f"Role {name} requires the finish capability")
        if "verify" in caps and workspace != "write":
            raise ConfigError("verify requires a writable workspace")
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
                           boolean(role.get("persistent", False), "persistent"),
                           boolean(role.get("on_change", False), "on_change"), interval, calendar, daemon)
    if not roles:
        raise ConfigError("At least one role is required")
    consult = strings(data.get("consult_profiles", []), "consult_profiles")
    if any(p not in profiles for p in consult) or len(set(consult)) != len(consult):
        raise ConfigError("consult_profiles must contain unique, configured profiles")
    timezone = string(data.get("timezone", "UTC"), "timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone: {timezone}") from exc
    web = data.get("web", {})
    keys(web, {"hosts", "feeds", "cache_seconds", "timeout_seconds", "max_bytes", "search_command"}, "web")
    web = {"hosts": [], "feeds": [], "cache_seconds": 1800, "timeout_seconds": 20,
           "max_bytes": 524288, "search_command": [], **web}
    for k in ("hosts", "feeds", "search_command"):
        strings(web[k], f"web.{k}")
    for k in ("cache_seconds", "timeout_seconds", "max_bytes"):
        number(web[k], f"web.{k}", 1, 16777216)
    def trusted_command(data, key: str, default: str) -> tuple[str, ...]:
        command = strings(data.get(key, [default]), key)
        if not command or any(not s or "\n" in s for s in command):
            raise ConfigError(f"{key} must be a nonempty argv array")
        return command
    command = trusted_command(data, "pi_command", "pi")
    codex_command = trusted_command(data, "codex_command", "codex")
    claude_command = trusted_command(data, "claude_command", "claude")
    return Config(file, path_value(string(data.get("data_dir", "~/.local/state/mizu"), "data_dir"), file.parent),
                  command, codex_command, claude_command,
                  path_value(data.get("pi_dir", "pi"), file.parent), limits, sandbox,
                  profiles, roles, consult, timezone, web,
                  strings(data.get("exclude", [".git", ".pi", ".env", ".env.*", ".venv",
                                                "node_modules", "__pycache__", ".pytest_cache"]), "exclude"))
