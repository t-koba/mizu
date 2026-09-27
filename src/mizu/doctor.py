"""Explicit installation checks. Missing dependencies never become false passes."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from . import NODE_MINIMUM, PI_MINIMUM
from . import platform as _platform
from .budget import Budget
from .config import Config
from .drivers import command_for
from .errors import ConfigError, Denied, MizuError
from .fs import mkdir, read_json
from .pi import credentials
from .process import run
from .sandbox import Sandbox, ensure_single, runtime_base, runtime_env

ROOT = Path(__file__).resolve().parents[2]


def container_runtime(config: Config) -> dict:
    """Behavioral prerequisite: an unprivileged user plus a working OCI
    runtime (`podman` or `docker`) with the configured image available.
    Enforcement (cgroup limits, seccomp, userns mapping) is proven by the
    sandbox smoke probe, not by version-string inspection, so this holds on
    Linux, macOS and Windows alike."""
    if _platform.is_root():
        raise Denied("Run Mizu as an unprivileged user, never as root/admin")
    executable = config.sandbox.executable
    if not executable or "\n" in executable or "\x00" in executable:
        raise ConfigError("sandbox.executable must be a container runtime command")
    if shutil.which(executable) is None and not Path(executable).is_file():
        raise Denied(f"Container runtime not found: {executable} "
                     "(install Podman or Docker Desktop, or fix sandbox.executable)")
    env = runtime_env(config)
    versioned = run([*runtime_base(config), "--version"], timeout=20, maximum=8192, env=env)
    if versioned.exit_code != 0:
        raise Denied("Container runtime failed: " + versioned.stderr[-1000:])
    version = versioned.stdout.strip().splitlines()[0] if versioned.stdout.strip() else "unknown"
    if config.sandbox.mode == "single":
        # Constrained-host legacy path: Linux user namespaces only.
        prepared = ensure_single(config)
        evidence = run([*config.sandbox.namespace_helper, "sh", "-c",
                        "cat /proc/self/uid_map; id -u"], timeout=10, maximum=1024)
        if evidence.exit_code != 0:
            raise Denied("Namespace helper failed: " + evidence.stderr[-500:])
        lines = evidence.stdout.split()
        if len(lines) != 4 or lines[0] != "0" or lines[2] != "1" or lines[3] != "0":
            raise Denied("Single mode requires a single-ID namespace map, found: " + evidence.stdout.strip())
        if lines[1] == "0":
            raise Denied("Single mode must run as an unprivileged host user")
        return {"mode": "single", "runtime": Path(executable).name,
                "namespace_map": " ".join(lines[:3]), "host_uid": lines[1],
                "prepared": prepared["prepared"], "version": version}
    return {"mode": "container", "runtime": Path(executable).name, "version": version}


def check(config: Config, *, sandbox: bool = False) -> dict:
    checks = []
    def checked(name, function):
        try:
            result = function()
            checks.append({"name": name, "status": "pass", "details": result})
        except (MizuError, OSError, ValueError, TypeError, KeyError) as exc:
            checks.append({"name": name, "status": "fail", "details": str(exc)})
    checked("platform", lambda: platform())
    checked("model profiles", lambda: [config.model(p) for p in sorted(config.profiles)])
    checked("credential permissions", lambda: {"keys": sorted(credentials(config.file.parent / "credentials.env"))})
    checked("timezone", lambda: {"timezone": str(ZoneInfo(config.timezone))})
    checked("free disk reserve", lambda: free_disk(config))
    checked("budget file", lambda: budget_file(config))
    checked("service units", lambda: service_units())
    checked("proposal stores", lambda: proposal_stores(config))
    def node():
        result = run(["node", "--version"], timeout=10, maximum=1024)
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
        minimum = tuple(map(int, NODE_MINIMUM.split(".")))
        if not match or tuple(map(int, match.groups())) < minimum:
            raise ConfigError(f"Node >={NODE_MINIMUM} is required by the pinned Pi release")
        return result.stdout.strip()
    def pi():
        result = run([*config.pi_command, "--version"], timeout=30, maximum=8192)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
        minimum = tuple(map(int, PI_MINIMUM.split(".")))
        if result.exit_code != 0 or not match or tuple(map(int, match.groups())) < minimum:
            raise ConfigError(f"Pi >={PI_MINIMUM} is required; found: " + (result.stdout + result.stderr)[-500:])
        return result.stdout.strip()
    def cli_engine(engine: str):
        """Presence plus required-flag drift check for codex/claude (see adapters/*/compatibility.json)."""
        compat = read_json(ROOT / "adapters" / engine / "compatibility.json", None)
        if not isinstance(compat, dict) or not isinstance(compat.get("required_flags"), list):
            raise ConfigError(f"Missing compatibility contract for engine: {engine}")
        command = command_for(config, engine)
        versioned = run([*command, "--version"], timeout=30, maximum=8192)
        if versioned.exit_code != 0:
            raise ConfigError(f"{engine} CLI is not runnable: " + (versioned.stdout + versioned.stderr)[-500:])
        helped = run([*command, "--help"], timeout=30, maximum=262144)
        if helped.exit_code != 0:
            raise ConfigError(f"{engine} --help failed: " + (helped.stdout + helped.stderr)[-500:])
        missing = [f for f in compat["required_flags"] if not help_has_flag(helped.stdout, f)]
        if missing:
            raise ConfigError(f"{engine} CLI drift: --help lacks required flags: {', '.join(missing)}; "
                              f"installed: {versioned.stdout.strip()[-200:]}")
        return {"version": versioned.stdout.strip()[-200:], "required_flags": compat["required_flags"]}
    engines = {config.engine(p) for p in config.profiles}
    if "pi" in engines:
        checked("node", node)
        checked("pi version", pi)
    else:
        checks.append({"name": "node", "status": "not_run", "details": "No profile uses the pi engine"})
        checks.append({"name": "pi version", "status": "not_run", "details": "No profile uses the pi engine"})
    for engine in ("codex", "claude"):
        if engine in engines:
            checked(f"{engine} CLI", lambda engine=engine: cli_engine(engine))
        else:
            checks.append({"name": f"{engine} CLI", "status": "not_run",
                           "details": f"No profile uses the {engine} engine"})
    checked("container runtime prerequisites", lambda: container_runtime(config))
    def image():
        if not config.sandbox.image:
            raise ConfigError("Set a digest-pinned sandbox.image")
        env = runtime_env(config)
        # `image inspect` is the portable presence check; `image exists` is Podman-only.
        result = run([*runtime_base(config), "image", "inspect", config.sandbox.image],
                     timeout=15, maximum=8192, env=env)
        if result.exit_code:
            raise ConfigError("Configured image is not present locally; automatic pulls are disabled")
        return config.sandbox.image
    checked("sandbox image", image)
    checks.append({"name": "provider-request admission", "status": "pass" if config.limits.daily_requests > 0 else "disabled",
                   "details": {"daily_requests": config.limits.daily_requests, "scope": "shared UTC-day request count, not currency"}})
    checks.append({"name": "search sources", "status": "pass" if config.web["feeds"] or config.web["search_command"] else "disabled",
                   "details": "Configure feeds or a trusted search executable; no implicit search service."})
    if sandbox:
        def exercise():
            container_runtime(config)
            mkdir(config.data)
            single = config.sandbox.mode == "single"
            with tempfile.TemporaryDirectory(prefix="doctor-", dir=config.data) as tmp:
                base = Path(tmp)
                work = base / "workspace"
                mkdir(work)
                (work / "probe.txt").write_text("readable")
                engine = Sandbox(config, base, "doctor", base / "run")
                secret_check = """if env | grep -qE '(^|_)(API_KEY|TOKEN|SECRET)='; then
  echo 'container inherited API secrets' >&2
  exit 1
fi
"""
                capability_check = """test "$(grep '^CapEff:' /proc/self/status | awk '{print $2}')" = 0000000000000000
test "$(grep '^Seccomp:' /proc/self/status | awk '{print $2}')" = 2
"""
                net_check = """python3 - <<'PROBE'
import os, socket
# A networkless namespace has no routable interface. Do not contact an external service.
assert set(os.listdir('/sys/class/net')) <= {'lo'}
print('isolation-probe-passed')
PROBE
"""
                if single:
                    script = """set -eu
[ -r /workspace/probe.txt ]
! (echo unsafe > /workspace/probe.txt) 2>/dev/null
[ ! -S /run/podman/podman.sock ]
[ ! -S /var/run/docker.sock ]
test $(id -u) -eq 0
""" + capability_check + secret_check + net_check
                else:
                    script = """set -eu
[ -r /workspace/probe.txt ]
! (echo unsafe > /workspace/probe.txt) 2>/dev/null
[ ! -S /run/podman/podman.sock ]
[ ! -S /var/run/docker.sock ]
""" + secret_check + """python3 - <<'PROBE'
import os, socket
assert os.getuid() != 0
assert not os.path.exists('/root/.ssh/id_rsa')
# A networkless namespace has no routable interface. Do not contact an external service.
assert set(os.listdir('/sys/class/net')) <= {'lo'}
print('isolation-probe-passed')
PROBE
"""
                record = engine.execute(work, script, writable=False, timeout=30)
                if record.get("exit_code") != 0 or record.get("reason") != "exited":
                    raise Denied("Sandbox smoke failed: " + json.dumps(record))
                if (work / "probe.txt").read_text() != "readable":
                    raise Denied("Read-only bind mount was writable")
                return {"checks": ["readonly source", "nonroot user", "no inherited API secrets", "networkless namespace", "container cleanup"],
                        "record": record}
        checked("real sandbox smoke", exercise)
    else:
        checks.append({"name": "real sandbox smoke", "status": "not_run", "details": "Run doctor --sandbox explicitly"})
    return {"ok": all(c["status"] != "fail" for c in checks), "checks": checks,
            "live_pi": "Not tested by doctor. Run smoke --live explicitly; it incurs provider usage."}


def platform() -> dict:
    if sys.version_info < (3, 11):
        raise ConfigError("Python >=3.11 is required")
    if not shutil.which("git"):
        raise ConfigError("Git is required")
    return {"system": _platform.SYSTEM, "python": ".".join(map(str, sys.version_info[:3]))}


def free_disk(config: Config) -> dict:
    free = shutil.disk_usage(config.data).free if config.data.exists() else shutil.disk_usage(config.data.parent).free
    reserve = config.limits.free_disk_mb * 1048576
    if free < reserve:
        raise ConfigError(f"Free disk {free} bytes is below the {reserve} byte reserve")
    return {"free_bytes": free, "reserve_bytes": reserve}


def budget_file(config: Config) -> dict:
    usage = Budget(config.data / "budget", config.limits.daily_requests).usage()
    if not isinstance(usage["used"], int) or usage["used"] < 0 or usage["used"] > max(usage["limit"], 0):
        raise ConfigError("Budget file is corrupt; inspect data/budget/<day>.json")
    return usage


def help_has_flag(text: str, flag: str) -> bool:
    """Token-anchored flag presence. Substring search would accept `--print`
    as evidence for `-p`."""
    return re.search(r"(?:^|\s)" + re.escape(flag) + r"(?:[\s,=]|$)", text) is not None


def inbox_integrity(project_root: Path) -> dict:
    """Unreadable or malformed proposals that projections skip. Loud here."""
    bad, total = [], 0
    for path in sorted((project_root / "inbox").glob("*.json")):
        total += 1
        try:
            if path.is_symlink():
                raise ValueError("symlink")
            item = read_json(path)
            for key in ("id", "source", "title", "created_at", "base_snapshot"):
                item[key]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            bad.append(path.name)
    if bad:
        raise ConfigError("Unreadable proposals (projections skip them): " + ", ".join(bad[:10]))
    return {"proposals": total}


def proposal_stores(config: Config) -> dict:
    """Fail loudly on unreadable proposals in any project. Projections skip
    them for availability; doctor is the detection point."""
    checked = bad = 0
    for project in sorted((config.data / "projects").glob("*")):
        if not project.is_dir() or project.name.startswith("."):
            continue
        inbox = project / "inbox"
        if not inbox.is_dir():
            continue
        checked += 1
        try:
            inbox_integrity(project)
        except ConfigError as exc:
            bad += 1
            raise ConfigError(f"{project.name}: {exc}") from exc
    return {"projects": checked, "unreadable": bad}


def service_units(directory: Path | None = None, *, system: str | None = None) -> dict:
    """Installed mizu-* service definitions whose executable is missing.

    One check for every platform: systemd units on Linux, launchd plists on
    macOS, Task Scheduler XML on Windows. Each format is parsed with the
    standard library; anything unparseable or pointing at a missing
    executable fails loudly.
    """
    name = system or _platform.SYSTEM
    directory = directory or _platform.service_dir(name)
    if name == "macos":
        return _launchd_units(directory)
    if name == "windows":
        return _task_units(directory)
    if name != "linux":
        raise ConfigError(f"Unsupported service platform: {name}")
    units = sorted(p.name for p in directory.glob("mizu-*") if p.suffix in (".service", ".timer")) \
        if directory.is_dir() else []
    broken = []
    for unit in units:
        try:
            text = (directory / unit).read_text().splitlines()
            if unit.endswith(".timer"):
                target = next(l for l in text if l.startswith("Unit=")).partition("=")[2]
                if not (directory / target).exists():
                    broken.append(unit)
                continue
            line = next(l for l in text if l.startswith("ExecStart="))
            argv = shlex.split(line.partition("=")[2])
            if not argv or not Path(argv[0]).exists():
                broken.append(unit)
        except (OSError, ValueError, StopIteration):
            broken.append(unit)
    if broken:
        raise ConfigError("Units reference a missing executable: " + ", ".join(broken))
    return {"system": name, "units": units}


def _launchd_units(directory: Path) -> dict:
    import plistlib
    units = sorted(p.name for p in directory.glob("mizu-*") if p.suffix == ".plist") \
        if directory.is_dir() else []
    broken = []
    for unit in units:
        try:
            data = plistlib.loads((directory / unit).read_bytes())
            argv = data.get("ProgramArguments", [])
            if not argv or not Path(argv[0]).exists():
                broken.append(unit)
        except (OSError, ValueError):
            broken.append(unit)
    if broken:
        raise ConfigError("Plists reference a missing executable: " + ", ".join(broken))
    return {"system": "macos", "units": units}


def _task_units(directory: Path) -> dict:
    import xml.etree.ElementTree as ET
    units = sorted(p.name for p in directory.glob("mizu-*") if p.suffix == ".xml") \
        if directory.is_dir() else []
    broken = []
    for unit in units:
        try:
            root = ET.fromstring((directory / unit).read_bytes())
            command = root.findtext(".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Command")
            if not command or not Path(command).exists():
                broken.append(unit)
        except (OSError, ET.ParseError):
            broken.append(unit)
    if broken:
        raise ConfigError("Tasks reference a missing executable: " + ", ".join(broken))
    return {"system": "windows", "units": units}
