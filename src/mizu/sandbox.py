"""One container per command. No privileged fallback.

Network isolation is policy, not a hardcoded ban: `[sandbox] network` defaults
to `none` and any other value is passed to the runtime explicitly, with the
choice recorded per command. Read-only root, dropped capabilities,
no-new-privileges, user mapping and seccomp floors stay fixed regardless.

The container interior is always Linux, so probes stay identical on every
host. The host side speaks generic OCI CLI (`podman` or `docker` via
`sandbox.executable`): only flags both runtimes accept are used, mounts use
`-v` on Linux (where `:` is unambiguous and `:z` relabeling applies) and
`--mount` elsewhere (where drive letters contain `:`).
"""
from __future__ import annotations

import dataclasses
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from . import platform as _platform
from .config import Config
from .errors import ConfigError, Denied
from .fs import SCRIPT_MAX, digest, mkdir, now, write_json
from .process import Result, environment, run

#: Single-mode cgroup parents already prepared in this process.
_PREPARED_SINGLE: set[str] = set()

#: Podman checkpoint image marker (CVE-2026-94603 / GHSA-2cvf-wqm6-wr9g).
#: On unpatched Podman (>= v4.4.0, < v5.8.8 / < v6.1.3) `podman run` restores
#: the checkpoint config and silently ignores user flags such as
#: `--cap-drop=ALL`. Checkpoint images are never valid sandbox images, so
#: the annotation is refused unconditionally (no version parsing, no knob).
CHECKPOINT_ANNOTATION = "io.podman.annotations.checkpoint.runtime.name"

#: Bound for the checkpoint `image inspect` probe. Full pretty-printed
#: inspect routinely exceeds 8 KiB, and a supply-chain author can pad
#: prefix fields to push the marker past any cut, so the probe must both
#: fit ordinary output and refuse truncated output (see below).
CHECKPOINT_INSPECT_MAXIMUM = 262144


def _has_checkpoint_key(node) -> bool:
    """Recursively search decoded `image inspect` JSON for the marker key."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == CHECKPOINT_ANNOTATION:
                return True
            if _has_checkpoint_key(value):
                return True
        return False
    if isinstance(node, (list, tuple)):
        return any(_has_checkpoint_key(item) for item in node)
    return False


def inspect_text_is_checkpoint(stdout: str) -> bool:
    """True when `image inspect` output carries the checkpoint marker.

    Schema/bounds: decoded JSON searched recursively; unparseable output
    falls back to a literal substring search so a format change cannot hide
    the marker. Trust: local runtime output from a complete (non-truncated)
    inspect only. Failure: False on complete output without the marker;
    the caller must deny truncated/failed inspects before trusting False.
    """
    try:
        return _has_checkpoint_key(json.loads(stdout))
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return CHECKPOINT_ANNOTATION in stdout


def inspect_text_has_valueless_env(stdout: str) -> bool:
    """True when `image inspect` output carries a valueless image Env entry.

    On unpatched Podman (GHSA-4hq8-gpf5-8p68) an image Config.Env entry
    with a bare key (no `=value`) or `*` copies host env into the
    container. Trust: decoded local runtime output only. Failure: False
    when output is clean or unparseable; the caller must deny
    truncated/failed inspects before trusting False.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return False
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "Env" and isinstance(value, list):
                    for entry in value:
                        if not isinstance(entry, str):
                            continue
                        if "=" not in entry or entry == "*" or entry.startswith("*="):
                            return True
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return False


def checkpoint_inspect_argv(config: Config) -> list[str]:
    """Portable presence+annotation probe: `image inspect <image>`."""
    return [*runtime_base(config), "image", "inspect", config.sandbox.image]


def assert_no_checkpoint(config: Config, env: dict[str, str]) -> None:
    """Fail closed when the configured image is a Podman checkpoint image.

    Schema/bounds: one bounded `image inspect` (15 s, 256 KiB). Trust:
    local runtime output only. Retry: none. Evidence: Denied message names
    the annotation, or the inspect reason/exit for unverifiable output.
    Failure: Denied on positive evidence and on any unverifiable inspect
    (truncated output, timeout, cancel, nonzero exit, missing runtime):
    a prefix cut can hide the marker, so truncated output must never read
    as clean.
    """
    if not config.sandbox.image:
        raise ConfigError("Build and pin sandbox.image before running commands")
    try:
        result = run(checkpoint_inspect_argv(config), timeout=15,
                     maximum=CHECKPOINT_INSPECT_MAXIMUM, env=env)
    except OSError as exc:
        raise Denied(
            "Cannot verify sandbox.image is not a Podman checkpoint image "
            "(inspect failed: %s); refusing to launch" % exc) from exc
    if result.reason != "exited" or result.exit_code is None or result.exit_code != 0:
        raise Denied(
            "Cannot verify sandbox.image is not a Podman checkpoint image "
            "(inspect %s%s); refusing to launch"
            % (result.reason,
               "" if result.exit_code in (None, 0)
               else " exit %s: %s" % (result.exit_code, result.stderr[-500:])))
    if inspect_text_is_checkpoint(result.stdout):
        raise Denied(
            "sandbox.image is a Podman checkpoint image (annotation %s); " % CHECKPOINT_ANNOTATION
            + "checkpoint config silently ignores sandbox flags on unpatched Podman "
            + "(CVE-2026-94603). Rebuild from a clean base and repin sandbox.image")
    if inspect_text_has_valueless_env(result.stdout):
        raise Denied(
            "sandbox.image has valueless Env (bare-key or `*` entry); "
            + "unpatched Podman copies host env into the container "
            + "(GHSA-4hq8-gpf5-8p68). Rebuild without bare-key Env or upgrade Podman >=5.8.4/>=6.0.0")


def runtime_base(config: Config) -> list[str]:
    """Trusted container invocation prefix: optional namespace wrapper plus
    storage/cgroup globals. Single mode keeps every path explicit so the
    command works under minimal service environments as well."""
    cfg = config.sandbox
    argv = list(cfg.namespace_helper)
    if cfg.namespace_helper and not (Path(argv[0]).is_file() and os.access(argv[0], os.X_OK)):
        raise Denied("sandbox.namespace_helper is not executable: " + argv[0])
    argv.append(cfg.executable)
    if cfg.mode == "single":
        argv.extend(["--root", cfg.podman_root, "--runroot", cfg.podman_runroot,
                     "--tmpdir", cfg.podman_tmpdir, "--cgroup-manager", cfg.cgroup_manager])
        if cfg.storage_driver:
            argv.extend(["--storage-driver", cfg.storage_driver])
        for opt in cfg.storage_options:
            argv.extend(["--storage-opt", opt])
    return argv


def runtime_env(config: Config) -> dict[str, str]:
    """Resolve helpers next to the configured runtime binary even when PATH
    is minimal, as in user services."""
    env = environment()
    bindir = str(Path(config.sandbox.executable).parent)
    path = env.get("PATH", os.defpath)
    first = [p for p in (bindir, *[str(Path(h).parent) for h in config.sandbox.namespace_helper]) if p]
    env["PATH"] = os.pathsep.join([*first, path]) if path else os.pathsep.join(first)
    return env


def bind_args(source: str, target: str, *, readonly: bool, selinux: bool, linux: bool) -> list[str]:
    """Bind-mount spelling both runtimes accept. `-v` on Linux keeps `:z`
    relabeling; `--mount` elsewhere avoids the `:` delimiter that Windows
    drive letters contain. `linux` is explicit so tests cover both spellings
    without mocking the host."""
    if linux:
        mode = ("ro" if readonly else "rw") + (",z" if selinux else "")
        return ["--volume", f"{source}:{target}:{mode}"]
    return ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]


def _is_podman(executable: str) -> bool:
    return Path(executable).name == "podman"


def remove_argv(config: Config, name: str) -> list[str] | None:
    """Portable leftover removal. Podman accepts `--ignore` for the common
    already-removed case; without it (Docker) removal needs an existence
    check first, so this returns None and the caller lists before removing."""
    if _is_podman(config.sandbox.executable):
        return [*runtime_base(config), "rm", "--force", "--ignore", name]
    return None


def remove_leftover(config: Config, name: str, env: dict[str, str]) -> str | None:
    """Remove one container by exact name without `--ignore`. Returns stderr
    on failure, None on success or when already absent."""
    listed = run([*runtime_base(config), "ps", "--all", "--filter", f"name={name}",
                  "--format", "{{.ID}}"], timeout=15, maximum=8192, env=env)
    if listed.exit_code != 0:
        return listed.stderr
    if not listed.stdout.split():
        return None
    removed = run([*runtime_base(config), "rm", "--force", name],
                  timeout=15, maximum=8192, env=env)
    if removed.exit_code != 0:
        return removed.stderr
    return None


def ensure_single(config: Config) -> dict:
    """Idempotently prepare single-mode host state: storage directories and
    the delegated cgroup that carries cpu/memory/pids controllers. Anything
    missing or unchangeable is a loud failure, never a silent downgrade."""
    if not _platform.IS_LINUX:
        raise Denied("Single mode requires Linux; use the default container mode elsewhere")
    cfg = config.sandbox
    parent = Path("/sys/fs/cgroup") / cfg.cgroup_parent
    key = "|".join([*(str(getattr(cfg, k)) for k in ("podman_root", "podman_runroot", "podman_tmpdir")),
                    str(parent)])
    if key in _PREPARED_SINGLE:
        return {"mode": "single", "prepared": [], "cgroup_parent": str(parent)}
    made = []
    for slot in ("podman_root", "podman_runroot", "podman_tmpdir"):
        path = Path(getattr(cfg, slot))
        if not path.is_dir():
            mkdir(path)
            made.append(str(path))
    if not parent.is_dir():
        try:
            parent.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise Denied("Cannot create single-mode cgroup parent %s: %s. Create it from a delegated slice first."
                         % (parent, exc)) from exc
        made.append(str(parent))
    try:
        enabled = (parent / "cgroup.subtree_control").read_text(encoding="utf-8").split()
    except OSError as exc:
        raise Denied("Cannot read cgroup controllers at %s: %s" % (parent, exc)) from exc
    for controller in ("cpu", "memory", "pids"):
        if controller not in enabled:
            try:
                with open(parent / "cgroup.subtree_control", "w") as handle:
                    handle.write("+" + controller)
            except OSError as exc:
                raise Denied("Controller %s is not delegable at %s: %s" % (controller, parent, exc)) from exc
    profile = Path(cfg.executable).parent / ".." / "share" / "containers" / "seccomp.json"
    if not profile.is_file():
        raise Denied("Single-mode seccomp profile is missing next to the podman binary")
    _PREPARED_SINGLE.add(key)
    return {"mode": "single", "prepared": made, "cgroup_parent": str(parent)}


class Sandbox:
    def __init__(self, config: Config, project: Path, role: str, run_dir: Path,
                 *, cancel: Callable[[], bool] = lambda: False):
        self.config, self.project, self.role, self.run_dir = config, project, role, run_dir
        self.cancel = cancel
        self.label = digest(str(project).encode())[:24]

    def base(self) -> list[str]:
        return runtime_base(self.config)

    def _validate_mount_paths(self, source: str, rejected: tuple[str, ...]) -> None:
        cfg = self.config.sandbox
        if any(x in source for x in rejected):
            raise ConfigError("Workspace path contains a container mount delimiter")
        for mount in cfg.mounts:
            if any(x in mount["source"] for x in rejected) or any(x in mount["target"] for x in rejected):
                raise ConfigError("Operator mount contains a container mount delimiter")
            if not Path(mount["source"]).exists():
                raise ConfigError(f"Operator mount source is missing: {mount['source']}")

    def _fixed_floor(self, name: str) -> list[str]:
        """Non-negotiable hardening: labels, user mapping, caps, resources."""
        cfg = self.config.sandbox
        command = [*self.base(), "run", "--rm", "--pull=never", "--name", name,
                   "--label", f"io.mizu.project={self.label}",
                   "--label", f"io.mizu.role={self.role}",
                   "--label", f"io.mizu.run={self.run_dir.name}"]
        if cfg.mode == "single":
            ensure_single(self.config)
            seccomp = str(Path(cfg.executable).parent / ".." / "share" / "containers" / "seccomp.json")
            command += ["--runtime-flag", "root=" + str(Path(cfg.podman_runroot) / "crun"),
                        "--cgroup-parent", cfg.cgroup_parent,
                        "--security-opt", "seccomp=" + seccomp,
                        "--uidmap", "0:0:1", "--gidmap", "0:0:1", "--user", "0:0"]
        else:
            command += ["--user", _platform.container_user()]
            # Rootless Podman without an explicit userns maps `--user <host uid>`
            # to a subordinate container id, leaving the bind-mounted workspace
            # unreadable. `--userns=keep-id` keeps the host uid inside the
            # container so the mount stays readable. Docker has no `keep-id`
            # mode, so the flag is Podman-only (see docs/setup.md).
            if _is_podman(cfg.executable):
                command += ["--userns=keep-id"]
        command += ["--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--memory", f"{cfg.memory_mb}m", "--memory-swap", f"{cfg.memory_mb}m",
                    "--cpus", str(cfg.cpus), "--pids-limit", str(cfg.pids),
                    "--ulimit", f"fsize={cfg.file_mb * 1024 * 1024}:{cfg.file_mb * 1024 * 1024}",
                    "--ulimit", "core=0:0", "--log-driver=none", "--init",
                    "--tmpfs", f"/tmp:rw,nosuid,nodev,size={cfg.temporary_mb}m,mode=1777",
                    "--env", "HOME=/tmp/home", "--env", "TMPDIR=/tmp",
                    "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPYCACHEPREFIX=/tmp/pycache"]
        return command

    def argv(self, name: str, workspace: Path, script: str, *, writable: bool,
             experiment: bool = False) -> list[str]:
        """Build the container argv from fixed floors plus policy selections.

        Schema/bounds: fixed floor (read-only root, cap-drop, no-new-privs,
        seccomp, user mapping, resource bounds) is non-negotiable; policy
        selections (network, entrypoint, mounts, env) come from `[sandbox]`
        and are recorded per command. Trust: config only, never model input.
        Retry: none (argv construction). Evidence: argv-adjacent record in
        execute(). Failure: ConfigError for missing image/paths.
        """
        cfg = self.config.sandbox
        if not cfg.image:
            raise ConfigError("Build and pin sandbox.image before running commands")
        source = str(workspace.resolve())
        linux = _platform.IS_LINUX
        # `-v` splits on `:` and `--mount` on `,`; reject the delimiter in use
        # (plus newline) so a path can never escape its mount. Allowing `:` off
        # Linux keeps Windows drive letters working.
        rejected = (":", "\n", ",") if linux else ("\n", ",")
        self._validate_mount_paths(source, rejected)
        readonly = not writable or experiment
        selinux = bool(cfg.selinux_label) and linux
        # --- Fixed mechanism floor (never operator-selectable) ---
        command = self._fixed_floor(name)
        # --- Operator policy selections (explicit grants, recorded per run) ---
        command += ["--network=" + cfg.network]
        command += [arg for key, value in sorted(cfg.env.items()) for arg in ("--env", f"{key}={value}")]
        command += bind_args(source, "/workspace", readonly=readonly,
                             selinux=selinux, linux=linux)
        for mount in cfg.mounts:
            command += bind_args(mount["source"], mount["target"],
                                 readonly=True, selinux=selinux, linux=linux)
        if experiment:
            command.extend(["--tmpfs", f"/work:rw,nosuid,nodev,size={cfg.temporary_mb}m,mode=1777",
                            "--workdir", "/work"])
        else:
            command.extend(["--workdir", "/workspace"])
        command.extend(["--entrypoint", cfg.entrypoint, cfg.image, "-c", script])
        return command

    def execute(self, workspace: Path, script: str, *, writable: bool,
                experiment: bool = False, timeout: int | None = None) -> dict:
        if not isinstance(script, str) or not script.strip() or len(script.encode()) > SCRIPT_MAX:
            raise Denied("Command must be nonempty and at most 64 KiB")
        # Clamp an explicit timeout to the configured command deadline.
        limit = self.config.limits.command_seconds
        timeout = limit if timeout is None else min(timeout, limit)
        operation = uuid.uuid4().hex
        name = f"mizu-{self.label}-{operation[:12]}"
        env = runtime_env(self.config)
        record = {"id": operation, "kind": "experiment" if experiment else "command",
                  "role": self.role, "created_at": now(), "script": script,
                  "image": self.config.sandbox.image, "container": name,
                  "network": self.config.sandbox.network, "writable": writable and not experiment,
                  "entrypoint": self.config.sandbox.entrypoint,
                  "mounts": [{"source": m["source"], "target": m["target"]}
                             for m in self.config.sandbox.mounts],
                  "env_keys": sorted(self.config.sandbox.env)}
        write_json(self.run_dir / "commands" / f"{operation}.started.json", record)
        try:
            assert_no_checkpoint(self.config, env)
            result = run(self.argv(name, workspace, script, writable=writable, experiment=experiment),
                         timeout=timeout, maximum=self.config.limits.output_bytes, cancel=self.cancel, env=env)
            record.update(dataclasses.asdict(result))
        except OSError as exc:
            record.update(dataclasses.asdict(Result(None, "", str(exc), "process_error", 0)))
        finally:
            # Killing only the launcher is insufficient: remove the container itself.
            ignored = remove_argv(self.config, name)
            if ignored is not None:
                cleanup = run(ignored, timeout=15, maximum=8192, env=env)
                if cleanup.exit_code != 0:
                    record["cleanup_error"] = cleanup.stderr
            else:
                error = remove_leftover(self.config, name, env)
                if error:
                    record["cleanup_error"] = error
        record["finished_at"] = now()
        write_json(self.run_dir / "commands" / f"{operation}.json", record)
        if record.get("cleanup_error"):
            raise Denied("Container cleanup failed; operator recovery is required")
        return record


def cleanup(config: Config, project: Path, role: str | None = None) -> dict:
    label = digest(str(project).encode())[:24]
    env = runtime_env(config)
    argv = [*runtime_base(config), "ps", "--all", "--filter", f"label=io.mizu.project={label}"]
    if role:
        argv.extend(["--filter", f"label=io.mizu.role={role}"])
    result = run([*argv, "--format", "{{.ID}}"], timeout=15, maximum=65536, env=env)
    if result.exit_code != 0:
        raise Denied(f"Cannot inspect leftover containers: {result.stderr}")
    ids = result.stdout.split()
    for cid in ids:
        if not all(c in "0123456789abcdef" for c in cid):
            raise Denied("Unexpected container identifier")
    if ids:
        result = run([*runtime_base(config), "rm", "--force", *ids], timeout=30, maximum=65536, env=env)
        if result.exit_code != 0:
            raise Denied(f"Container cleanup failed: {result.stderr}")
    return {"removed": len(ids)}
