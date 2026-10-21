"""Explicit installation checks. Missing dependencies never become false passes."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from . import platform as _platform
from .budget import Budget
from .config import Config, resolve_timezone
from .drivers import command_for
from .errors import ConfigError, Denied, MizuError
from .fs import digest, mkdir, read_json
from .pi import credentials
from .process import run, stop_note
from .sandbox import CHECKPOINT_ANNOTATION, CHECKPOINT_INSPECT_MAXIMUM, Sandbox, ensure_single, inspect_text_has_valueless_env, inspect_text_is_checkpoint, runtime_base, runtime_env
# The host-relative bound's shape and the record it is derived from are
# re-exported here: the installed-adapter contract check and the
# regressions read the one shape from the product, so no surface keeps a
# second copy of it.
from .startup import (ADAPTER_STARTUP_CEILING_SECONDS, ADAPTER_STARTUP_FLOOR_SECONDS,
                      ADAPTER_STARTUP_MEASURE_CEILING_SECONDS, ADAPTER_STARTUP_MULTIPLIER,
                      adapter_startup_bound, measured_startup, observed_startup,
                      record_startup, startup_bound)

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
    probe_outcome(versioned.reason, versioned.exit_code,
                  stop_note(versioned) or versioned.stderr, "Container runtime probe", 20)
    if versioned.exit_code != 0:
        # A run reports its exit only after every pipe thread stopped, so a
        # zero exit means the version output was read to its end. A child
        # that did not complete never said the runtime was missing.
        raise Denied("Container runtime failed (exit %s): " % versioned.exit_code
                     + (stop_note(versioned) or versioned.stderr[-1000:]))
    version = versioned.stdout.strip().splitlines()[0] if versioned.stdout.strip() else "unknown"
    if config.sandbox.mode == "single":
        # Constrained-host path: Linux user namespaces only.
        prepared = ensure_single(config)
        evidence = run([*config.sandbox.namespace_helper, "sh", "-c",
                        "cat /proc/self/uid_map; id -u"], timeout=10, maximum=1024)
        probe_outcome(evidence.reason, evidence.exit_code,
                      stop_note(evidence) or evidence.stderr, "Namespace helper probe", 10)
        if evidence.exit_code != 0:
            raise Denied("Namespace helper failed (exit %s): " % evidence.exit_code
                         + (stop_note(evidence) or evidence.stderr[-500:]))
        lines = evidence.stdout.split()
        if len(lines) != 4 or lines[0] != "0" or lines[2] != "1" or lines[3] != "0":
            raise Denied("Single mode requires a single-ID namespace map, found: " + evidence.stdout.strip())
        if lines[1] == "0":
            raise Denied("Single mode must run as an unprivileged host user")
        return {"mode": "single", "runtime": Path(executable).name,
                "namespace_map": " ".join(lines[:3]), "host_uid": lines[1],
                "prepared": prepared["prepared"], "version": version}
    return {"mode": "container", "runtime": Path(executable).name, "version": version,
            "host_unprivileged": True, "runtime_rootless": "not_verified"}


#: Host-relative bound for one adapter child. The launcher and the app-server
#: probe are plain children of this process and their start-up is dominated by
#: loading their SDK, so a fixed number cannot say how long they may take: on
#: a loaded host the child needs longer than the bound it was given, it is
#: killed before it can report, and the caller reads an empty refusal instead
#: of the child's answer. The bound is this host's own measured start-up cost,
#: scaled, with a floor and a ceiling -- the same shape, floor and ceiling the
#: launcher harness uses for the identical child, so the product and the
#: harness never hold two answers for one child's start-up.
def probe_outcome(reason, exit_code, detail, label: str, bound: int | None = None) -> None:
    """One classification for every probe the product runs.

    A probe that was killed, timed out or stopped never said what it holds,
    so its silence is not a missing capability: the reason, the exit code and
    the stderr are named instead, and the two remedies -- provision the
    dependency, or give this host more room -- stay distinguishable. The
    contract check and the doctor share this rule, so a child reads the same
    way wherever it is probed.

    A child that a signal stopped is the reachable shape of that silence:
    the process reports it as exited with the signal's number as its exit
    code, so the exit code below zero is what names the kill.
    """
    if reason == "exited" and exit_code is not None and exit_code >= 0:
        return
    text = (detail or "").strip()
    if reason == "timeout" and bound:
        raise ConfigError(f"{label} exceeded its {bound} s bound (timeout) and was stopped "
                          "before it could report"
                          + (f": {text[-1000:]}" if text else ""))
    if exit_code is not None and exit_code < 0:
        # A child a signal stopped never said what it holds, so its silence
        # is not a missing capability: the signal that stopped it is named
        # instead, so the operator is not sent to install the dependency.
        raise ConfigError(f"{label} was killed by signal {-exit_code} before it could report"
                          + (f": {text[-1000:]}" if text else ""))
    raise ConfigError(f"{label} did not complete ({reason}) and was stopped "
                      "before it could report"
                      + (f": {text[-1000:]}" if text else ""))


def probe_label(engine: str | None) -> str:
    """The name one probe child is given by every surface that probes it.

    The doctor and the contract check hold the identical child to the
    identical bound, so a refusal names the identical child: the name is
    derived from the engine here, once, instead of written out per call
    site where two surfaces could drift apart.
    """
    if engine == "codex":
        return "Codex app-server stdio probe"
    if engine:
        return f"{engine} adapter contract probe"
    return "Adapter contract probe"


def adapter_probe(config, engine, command, *, maximum: int, label: str):
    """Run one adapter child under this host's bound and name its outcome.

    The child is measured once under the ceiling and the probe that follows
    is held to the bound that measurement requires: the same order, and the
    same shape, the launcher harness uses for the identical child. A child
    that answered is measured by its answer, whatever that answer was: the
    launcher harness measures the identical child by its first answer, which
    is a refusal, so the two surfaces never hold two answers for one
    child's start-up. A child that does not answer inside the bound is a
    bound refusal with its own reason, never a missing capability: it was
    stopped before it could report, so nothing is known about the
    capabilities it holds.

    Returns the probe's result and the bound the child was held to, so the
    evidence names the bound that was applied.
    """
    measured = run(command, timeout=ADAPTER_STARTUP_MEASURE_CEILING_SECONDS, maximum=maximum)
    if measured.reason == "exited" and measured.exit_code is not None and measured.exit_code >= 0:
        # The child answered, and an answer is a measurement even when the
        # answer was a failure: the launcher harness measures the identical
        # child by its first answer, which is a refusal. The measurement
        # joins this host's record for the engine, so the bound is the one
        # every later start of that engine's child uses -- the runtime and
        # the doctor never hold two answers for one child's start-up -- and
        # the cost published beside it is the cost that bound was derived
        # from, so the published pair is checkable against the rule it
        # reports.
        cost, bound = observed_startup(config, engine, measured.seconds)
    else:
        # The measurement could not be taken: an unmeasured cost keeps the
        # floor, the same answer the launcher harness gives for the identical
        # child, and no cost is published for a child that did not answer.
        bound = ADAPTER_STARTUP_FLOOR_SECONDS
        cost = None
    result = run(command, timeout=bound, maximum=maximum)
    probe_outcome(result.reason, result.exit_code,
                  stop_note(result) or result.stderr, label, bound)
    return result, bound, cost


def startup_evidence(cost, bound: int) -> dict:
    """This host's measured cost for one adapter child and the bound it needs.

    The cost is the one the bound was derived from, so the published pair
    satisfies the published rule: the bound is twelve times the cost, at
    least the floor and at most the ceiling. A cost that could not be
    measured is published as no cost at all: the bound is then the floor,
    because a child that has not answered cannot claim a wider one.
    """
    if cost is None:
        return {"startup_seconds": None,
                "host_relative_bound_seconds": bound,
                "startup_unmeasured": True}
    return {"startup_seconds": round(float(cost), 3),
            "host_relative_bound_seconds": bound}


def engine_contract(config: Config, engine: str) -> dict:
    """The adapter's own capability report, under this host's probe bound."""
    from .engine_config import adapter_contract
    contract = adapter_contract(engine)
    command = config.command(engine)
    if engine == "codex":
        result, bound, cost = adapter_probe(config, engine,
                                            [*command, contract["command"][0], "--help"],
                                            maximum=262144, label=probe_label(engine))
        flags = list(contract["required_flags"])
        for item in contract.get("contracts", []):
            flags.extend(item["required_flags"])
        missing = [flag for flag in dict.fromkeys(flags) if not help_has_flag(result.stdout, flag)]
        if result.exit_code or missing:
            raise ConfigError("Codex app-server stdio capability missing: "
                              + ", ".join(missing or [stop_note(result)]))
        return {"request_unit": contract["request_unit"], "required_flags": sorted(set(flags)),
                "inference": "not_run", **startup_evidence(cost, bound)}
    launcher = ROOT / "adapters" / engine / contract["entrypoint"]
    result, bound, cost = adapter_probe(config, engine,
                                        [*command, str(launcher), "--check-contract"],
                                        maximum=8192, label=probe_label(engine))
    if result.exit_code:
        raise ConfigError("Required engine SDK capabilities missing: "
                          + (stop_note(result) or result.stderr[-1000:]))
    try:
        reported = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigError(f"Unreadable {engine} contract report: {exc}") from exc
    verified = reported.get("exports", []) if isinstance(reported, dict) else []
    missing = [name for name in contract["exports"] if name not in verified]
    if missing:
        raise ConfigError(f"Adapter drift: {engine} contract declares missing exports: " + ", ".join(missing))
    return {"request_unit": contract["request_unit"], "exports": sorted(set(verified)),
            "inference": "not_run", **startup_evidence(cost, bound)}


def sandbox_image(config: Config) -> str:
    """The configured sandbox image, verified against the local runtime.

    The child that inspected the image decides its own outcome: a child a
    signal stopped never said the image was missing, and a truncated
    inspect is unverifiable, so neither is read as a clean verdict.
    """
    if not config.sandbox.image:
        raise ConfigError("Set a digest-pinned sandbox.image")
    env = runtime_env(config)
    # `image inspect` is the portable presence check; `image exists` is Podman-only.
    # A prefix cut can hide the checkpoint marker, so any truncated or
    # failed inspect is unverifiable and must fail, never read as clean.
    result = run([*runtime_base(config), "image", "inspect", config.sandbox.image],
                 timeout=15, maximum=CHECKPOINT_INSPECT_MAXIMUM, env=env)
    # The child that inspected the image decides its own outcome: a
    # child a signal stopped never said the image was missing.
    probe_outcome(result.reason, result.exit_code,
                  stop_note(result) or result.stderr, "Sandbox image inspect probe", 15)
    if result.exit_code != 0:
        raise ConfigError("Configured image is not present locally; automatic pulls are disabled")
    if inspect_text_is_checkpoint(result.stdout):
        raise ConfigError(
            "Configured image is a Podman checkpoint image (annotation %s); " % CHECKPOINT_ANNOTATION
            + "it silently ignores sandbox flags on unpatched Podman (CVE-2026-94603). "
            + "Rebuild from a clean base and repin sandbox.image")
    if inspect_text_has_valueless_env(result.stdout):
        raise ConfigError(
            "Configured image has valueless Env (bare-key or `*` entry); "
            + "unpatched Podman copies host env into the container (GHSA-4hq8-gpf5-8p68). "
            + "Rebuild without bare-key Env or upgrade Podman >=5.8.4/>=6.0.0")
    return config.sandbox.image


def check(config: Config, *, sandbox: bool = False) -> dict:
    checks = []
    def checked(name, function):
        try:
            result = function()
            checks.append({"name": name, "status": "pass", "details": result})
        except (MizuError, OSError, ValueError, TypeError, KeyError) as exc:
            checks.append({"name": name, "status": "fail", "details": str(exc)})
    checked("platform", lambda: platform())
    git_status, git_details = git_presence()
    checks.append({"name": "git", "status": git_status, "details": git_details})
    checked("model profiles", lambda: [config.model(p) for p in sorted(config.profiles)])
    checked("credential permissions", lambda: {"keys": sorted(credentials(config.file.parent / "credentials.env"))})
    checked("timezone", lambda: {"timezone": str(resolve_timezone(config.timezone))})
    checked("free disk reserve", lambda: free_disk(config))
    checked("budget file", lambda: budget_file(config))
    checked("service units", lambda: service_units())
    checked("proposal stores", lambda: proposal_stores(config))
    def node():
        result = run([*config.command('pi'), "--input-type=module", "-e",
                      "import {createRequire} from 'node:module'; import {ReadableStream} from 'node:stream/web'; if(typeof createRequire!=='function'||typeof ReadableStream!=='function')process.exit(1); console.log('module and stream capabilities available')"], timeout=10, maximum=1024)
        probe_outcome(result.reason, result.exit_code,
                      stop_note(result) or result.stderr, "Node capability probe", 10)
        if result.exit_code:
            # The child answered and reported a failure: name the failure it
            # reported, not a capability it never claimed to be missing.
            raise ConfigError('Required Node module/stream capabilities missing'
                              + (' (exit %s)' % result.exit_code)
                              + (': ' + stop_note(result) if stop_note(result) else ''))
        return result.stdout.strip()
    engines = {config.engine(p) for p in config.profiles}
    if "pi" in engines or "pi-durable" in engines:
        checked("node", node)
    for engine in ("pi", "pi-durable", "codex", "claude"):
        if engine in engines:
            checked(f"{engine} adapter", lambda engine=engine: engine_contract(config, engine))
        else:
            checks.append({"name": f"{engine} adapter", "status": "not_run", "details": "No profile uses this engine"})
    from .engine_config import effective
    from .selection import role_profiles
    checked("engine resources", lambda: [effective(config, role, profile)["resources"]
            for role in config.roles.values() for profile in role_profiles(config, role)])
    checked("container runtime prerequisites", lambda: container_runtime(config))
    checked("sandbox image", lambda: sandbox_image(config))
    checks.append({"name": "provider-request admission", "status": "pass" if config.limits.daily_requests > 0 else "disabled",
                   "details": {"daily_requests": config.limits.daily_requests,
                             "shared_daily_requests": config.limits.shared_daily_requests,
                             "scope": f"per-project day request count in {config.timezone} plus optional shared total, not currency"}})
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
                net_enabled = config.sandbox.network != "none"
                net_check = """python3 - <<'PROBE'
import os, socket
# Operator-enabled network: only record the choice, probe nothing external.
print('network-operator-enabled')
PROBE
""" if net_enabled else """python3 - <<'PROBE'
import os, socket
# A networkless namespace has no routable interface. Do not contact an external service.
assert set(os.listdir('/sys/class/net')) <= {'lo'}
print('isolation-probe-passed')
PROBE
"""
                net_label = f"operator-enabled network ({config.sandbox.network})" if net_enabled else "networkless namespace"
                resource_check = """python3 - <<'RESOURCE'
import pathlib, json
root = pathlib.Path('/sys/fs/cgroup')
assert (root / 'cgroup.controllers').exists(), 'cgroup v2 resource evidence unavailable'
def value(name):
    return (root / name).read_text(encoding="utf-8").strip()
evidence = {name: value(name) for name in ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')}
assert int(evidence['memory.max']) == MEMORY_LIMIT
assert int(evidence['memory.swap.max']) == 0
assert int(evidence['pids.max']) == PID_LIMIT
quota, period = map(int, evidence['cpu.max'].split())
assert abs(quota / period - CPU_LIMIT) < 0.001
print(json.dumps({'resource_limits': evidence}, sort_keys=True))
RESOURCE
""".replace('MEMORY_LIMIT', str(config.sandbox.memory_mb * 1048576)).replace('PID_LIMIT', str(config.sandbox.pids)).replace('CPU_LIMIT', str(config.sandbox.cpus))
                script = """set -eu
[ -r /workspace/probe.txt ]
! (echo unsafe > /workspace/probe.txt) 2>/dev/null
[ ! -S /run/podman/podman.sock ]
[ ! -S /var/run/docker.sock ]
""" + ("test $(id -u) -eq 0\n" if single else "test $(id -u) -ne 0\n") + capability_check + secret_check + net_check + resource_check
                record = engine.execute(work, script, writable=False, timeout=30)
                if record.get("exit_code") != 0 or record.get("reason") != "exited":
                    raise Denied("Sandbox smoke failed: " + json.dumps(record))
                if (work / "probe.txt").read_text(encoding="utf-8") != "readable":
                    raise Denied("Read-only bind mount was writable")
                return {"checks": ["readonly source", "single-ID namespace user" if single else "nonroot container user", "zero effective capabilities", "seccomp filter active", "cgroup v2 memory/swap/pids/CPU limits", "no inherited API secrets", net_label, "container cleanup"],
                        "record": record}
        checked("real sandbox smoke", exercise)
    else:
        checks.append({"name": "real sandbox smoke", "status": "not_run", "details": "Run doctor --sandbox explicitly"})
    return {"ok": all(c["status"] != "fail" for c in checks), "checks": checks,
            "live_pi": "Not tested by doctor. Run smoke --live explicitly; it incurs provider usage."}


def platform() -> dict:
    if sys.version_info < (3, 11):
        raise ConfigError("Python >=3.11 is required")
    return {"system": _platform.SYSTEM, "python": ".".join(map(str, sys.version_info[:3]))}


def git_presence() -> tuple:
    """Report git availability without failing the check.

    Git is required only by git-tree project import (which fails with a
    reason before running git when it is missing). Nothing else in the
    product shells out to git, so a missing git is not applicable rather
    than a failure here.
    """
    found = shutil.which("git")
    if found:
        return "pass", {"git": found}
    return ("not_run",
            "Git is not on PATH; no selected operation needs it "
            "(git-tree project import requires it and fails with a reason when missing)")


def free_disk(config: Config) -> dict:
    free = shutil.disk_usage(config.data).free if config.data.exists() else shutil.disk_usage(config.data.parent).free
    reserve = config.limits.free_disk_mb * 1048576
    if free < reserve:
        raise ConfigError(f"Free disk {free} bytes is below the {reserve} byte reserve")
    return {"free_bytes": free, "reserve_bytes": reserve}


def budget_file(config: Config) -> dict:
    """Diagnose the budget ledger with scope-correct limits.

    Each scope is judged against its own cap -- per-project counts against
    ``daily_requests``, the shared day total against
    ``shared_daily_requests`` -- so projects whose sum exceeds one
    project's cap never read as corruption. Over-limit counts are
    legitimate exhaustion (or a later config reduction), reported as data
    for admission to enforce; only a malformed ledger shape fails.
    """
    audit = Budget(config.data / "budget", config.limits.daily_requests,
                   config.limits.retention_days,
                   config.limits.shared_daily_requests, config.timezone).audit()
    if audit["malformed"]:
        raise ConfigError("Budget file is corrupt; inspect data/budget/<day>.json: "
                          + ", ".join(audit["malformed"]))
    return audit


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
        return _check_units(directory, "mizu-*.plist", name, "Plists", _launchd_executable)
    if name == "windows":
        return _check_units(directory, "mizu-*.xml", name, "Tasks", _task_executable)
    if name != "linux":
        raise ConfigError(f"Unsupported service platform: {name}")
    return _check_units(directory, ("mizu-*.service", "mizu-*.timer", "mizu-*.path"), name, "Units", _systemd_executable)


def _check_units(directory: Path, patterns, system: str, noun: str,
                 extract) -> dict:
    """Shared unit loop: list, extract executable per unit, fail loudly.

    Schema/bounds: `mizu-*` glob(s). Trust: local unit files.
    Retry: none. Evidence: unit names. Failure: ConfigError names broken units.
    `extract` maps unit path to an argv/command list (empty means broken).
    """
    if isinstance(patterns, str):
        patterns = (patterns,)
    units: list[str] = []
    for pattern in patterns:
        if directory.is_dir():
            units.extend(p.name for p in directory.glob(pattern))
    units = sorted(set(units))
    broken = []
    for unit in units:
        try:
            argv = extract(directory / unit)
            if not argv or not Path(argv[0] if isinstance(argv, list) else argv).exists():
                broken.append(unit)
        except (OSError, ValueError, StopIteration):
            broken.append(unit)
        except Exception:
            broken.append(unit)
    if broken:
        raise ConfigError(f"{noun} reference a missing executable: " + ", ".join(broken))
    return {"system": system, "units": units}


def _systemd_executable(path: Path):
    text = path.read_text(encoding="utf-8").splitlines()
    if path.name.endswith((".timer", ".path")):
        target = next(l for l in text if l.startswith("Unit=")).partition("=")[2]
        return [str(path.parent / target)] if (path.parent / target).exists() else []
    line = next(l for l in text if l.startswith("ExecStart="))
    return shlex.split(line.partition("=")[2])


def _launchd_executable(path: Path):
    import plistlib
    return plistlib.loads(path.read_bytes()).get("ProgramArguments", [])


def _task_executable(path: Path):
    import xml.etree.ElementTree as ET
    command = ET.fromstring(path.read_bytes()).findtext(
        ".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Command")
    return [command] if command else []
