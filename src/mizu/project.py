"""Project lifecycle and operator-owned control files, separate from workspaces."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import tomllib
import uuid
from pathlib import Path

from .budget import Budget
from .config import Config, keys, strings
from .errors import ConfigError, Denied
from .fs import atomic_write, identifier, lock, mkdir, now, read_json, sync_dir, write_json
from .insights import Insights
from .process import environment, run
from .snapshot import open_store


class Project:
    def __init__(self, config: Config, name: str):
        self.config, self.name = config, identifier(name)
        self.root = config.data / "projects" / name
        self.workspace = self.root / "workspace"
        try:
            self.settings = tomllib.loads((self.root / "project.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Project is not initialized: {name}") from exc
        keys(self.settings, {"roles", "verify", "attributes"}, "project")
        from .selection import attributes
        attributes(self.settings.get("attributes", {}))
        self.roles = strings(self.settings.get("roles", []), "project.roles")
        self.verify = check_verify(self.settings.get("verify", []))
        if not self.roles or any(r not in config.roles for r in self.roles):
            raise ConfigError("Project roles must refer to configured roles")
        self.snapshots = open_store(self.root, config)
        self.insights = Insights(self.root, retention_days=config.limits.retention_days)
        self.insights.snapshots = self.snapshots
        from .role_state import RoleStateStore
        self.role_state = RoleStateStore(self.root / "role-state",
                                         max_bytes=config.limits.role_state_bytes)

    @property
    def goal(self) -> str:
        return read_goal(self.root / "PROJECT.md", "PROJECT.md must contain a nonempty goal within 64 KiB")

    def control(self) -> dict:
        return read_json(self.root / "control.json", {"armed": False, "paused": True, "wake_generation": "", "draining": False})

    def set_control(self, **updates) -> dict:
        with lock(self.root / "locks" / "control.lock"):
            value = {**self.control(), **updates, "updated_at": now()}
            write_json(self.root / "control.json", value)
            return value

    def reset_health(self) -> dict:
        """Reset per-role consecutive failure counters (resume rebase).

        Schema: returns ``{role: 0}`` for each reset record. Bounds: health
        dir only, ``*.json`` regular files. Trust: local operator state.
        Retry: best-effort per file, never raises. Evidence: rewritten health
        records keep ``last_run`` and note the reset. Failure: never raises;
        unreadable files are skipped.
        """
        reset: dict = {}
        try:
            paths = list((self.root / "health").glob("*.json"))
        except OSError:
            return reset
        for path in paths:
            try:
                if path.is_symlink():
                    continue
                record = read_json(path, {})
                if not isinstance(record, dict):
                    continue
                record = {**record, "consecutive_failures": 0,
                          "updated_at": now(), "resumed": True}
                write_json(path, record)
                reset[path.stem] = 0
            except (OSError, ValueError, TypeError):
                continue
        return reset

    def status(self) -> dict:
        snapshot = self.snapshots.get()
        budget = Budget(self.config.data / "budget", self.config.limits.daily_requests,
                        self.config.limits.retention_days,
                        self.config.limits.shared_daily_requests, self.config.timezone).usage(self.name)
        return {"project": self.name, "control": self.control(), "snapshot": snapshot["id"],
                "code_digest": snapshot["code_digest"], "created_at": snapshot["created_at"],
                "outcome": snapshot["outcome"], "summary": snapshot["summary"],
                "verification": snapshot["verification"], "state": snapshot["state"],
                "goal_digest": snapshot.get("goal_digest"), "wake_at": snapshot.get("wake_at"),
                "health": {p.stem: read_json(p) for p in (self.root / "health").glob("*.json") if not p.is_symlink()},
                "active": {p.stem: read_json(p) for p in (self.root / "active").glob("*.json") if not p.is_symlink()},
                "pending_insights": self.insights.list(limit=self.config.limits.pending_insights),
                "budget": {"used_requests": budget["used"], "limit_requests": budget["limit"],
                           "shared_used_requests": budget["shared_used"],
                           "shared_limit_requests": budget["shared_limit"],
                           "day": budget["day"], "bytes": budget["bytes"],
                           "reaped": budget["reaped"]}}


#: Maximum operator acceptance commands per project (each spawns a container).
MAX_VERIFY_COMMANDS = 32


def check_verify(value) -> tuple[str, ...]:
    """Validate acceptance commands: typed, counted, and TOML-safe."""
    commands = strings(value, "project.verify")
    if len(commands) > MAX_VERIFY_COMMANDS:
        raise ConfigError("project.verify holds too many commands")
    if any("\x00" in command or "\n" in command for command in commands):
        raise ConfigError("project.verify commands must not contain NUL or newlines")
    return commands


def read_goal(path: Path, complaint: str) -> str:
    """Read an operator goal file with a pre-read size cap, then validate."""
    try:
        oversized = path.stat().st_size > 65536
    except OSError:
        oversized = False
    if oversized:
        raise ConfigError(complaint)
    value = path.read_text(encoding="utf-8")
    if not value.strip() or len(value.encode()) > 65536:
        raise ConfigError(complaint)
    return value


def git_env() -> dict[str, str]:
    return environment(extra={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                              "GIT_TERMINAL_PROMPT": "0"})


def config_managed_repo(workspace: Path, env: dict[str, str]) -> None:
    for key, value in (("user.name", "Mizu"), ("user.email", "automation@localhost"),
                       ("core.hooksPath", os.devnull)):
        if run(["git", "-C", str(workspace), "config", key, value],
               timeout=15, maximum=8192, env=env).exit_code != 0:
            raise Denied("Cannot configure managed repository")


def init_managed_repo(workspace: Path) -> None:
    """Initialize the managed worktree repository. Loud failure, never partial trust."""
    env = git_env()
    if run(["git", "init", "--initial-branch=work", str(workspace)],
           timeout=15, maximum=8192, env=env).exit_code:
        raise Denied("Cannot initialize managed repository")
    config_managed_repo(workspace, env)


def _import_git_tree(source: Path, workspace: Path, env: dict[str, str]) -> None:
    """Clone a clean git tree without history leakage. Loud failure only."""
    status = run(["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
                  "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
                 # Only emptiness matters; truncation preserves truthiness.
                 timeout=30, maximum=65536, env=env)
    if status.exit_code != 0 or status.stdout.strip():
        raise Denied("Git import requires a clean source tree; commit or make a separate plain-directory export first")
    result = run(["git", "-c", f"core.hooksPath={os.devnull}", "clone", "--no-local",
                  "--no-hardlinks", "--", str(source), str(workspace)],
                 timeout=300, maximum=1048576, env=env)
    if result.exit_code != 0:
        raise Denied(f"Repository import failed: {result.stderr}")
    run(["git", "-C", str(workspace), "remote", "remove", "origin"],
        timeout=15, maximum=8192, env=env)
    config_managed_repo(workspace, env)


def _import_plain_directory(source: Path, workspace: Path, stage: Path, config: Config) -> None:
    """Copy only the visible snapshot, not caches or secrets."""
    temporary_store = open_store(stage, config)
    captured = temporary_store.capture_files(source)
    if captured["skipped"]:
        raise Denied("Plain-directory import contains unsupported links or special files: " + ", ".join(captured["skipped"][:10]))
    temporary_store.materialize(captured, workspace)
    init_managed_repo(workspace)


def initialize(config: Config, name: str, source: Path, goal_file: Path,
               roles: list[str], verification: list[str], *, armed: bool = False) -> Project:
    identifier(name)
    source, goal_file = source.resolve(), goal_file.resolve()
    if not source.is_dir() or not goal_file.is_file():
        raise ConfigError("Source directory and goal file must exist")
    goal = read_goal(goal_file, "Goal must be nonempty and at most 64 KiB")
    if not roles or any(role not in config.roles for role in roles):
        raise ConfigError("Choose existing roles")
    check_verify(verification)
    parent = config.data / "projects"
    mkdir(parent)
    destination = parent / name
    with lock(config.data / "locks" / f"init-{name}.lock", blocking=False):
        if destination.exists():
            raise Denied("Project already exists; initialization never overwrites it")
        stage = Path(tempfile.mkdtemp(prefix=".init-", dir=parent))
        try:
            env = git_env()
            if (source / ".git").exists():
                _import_git_tree(source, stage / "workspace", env)
            else:
                _import_plain_directory(source, stage / "workspace", stage, config)
            atomic_write(stage / "PROJECT.md", goal.encode())
            text = "roles = " + json.dumps(roles) + "\nverify = " + json.dumps(verification) + "\n"
            atomic_write(stage / "project.toml", text.encode())
            write_json(stage / "control.json", {"armed": armed, "paused": not armed,
                                                "draining": False,
                                                "wake_generation": uuid.uuid4().hex, "updated_at": now(),
                                                "reason": "Operator initialized armed" if armed else "Initialized unarmed; operator arm required"})
            mkdir(stage / "spool" / "editor")
            atomic_write(stage / "spool/editor/.mizu-outbox", b"Mizu Editor proposal outbox\n")
            store = open_store(stage, config)
            captured = store.capture_files(stage / "workspace")
            initial = store.create(captured, goal=goal, state="Initialized; no autonomous work has run.",
                                   run=None, outcome="wait", summary="Project initialized, unarmed.")
            store.publish(initial)
            os.rename(stage, destination)
            sync_dir(parent)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    return Project(config, name)
