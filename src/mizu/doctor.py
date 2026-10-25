"""Explicit installation checks. Missing dependencies never become false passes."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

from . import platform as _platform
from .budget import Budget
from .config import Config, keys, resolve_timezone, strings
from .drivers import command_for
from .errors import ConfigError, Denied, LimitExceeded, MizuError
from .fs import digest, identifier, mkdir, read_json, safe_read
from .pi import credentials
from .process import run, stop_note
from .sandbox import CHECKPOINT_ANNOTATION, CHECKPOINT_INSPECT_MAXIMUM, Sandbox, ensure_single, inspect_text_has_valueless_env, inspect_text_is_checkpoint, runtime_base, runtime_env
# The host-relative bound's shape and the record it is derived from are
# re-exported here: the installed-adapter contract check and the
# regressions read the one shape from the product, so no surface keeps a
# second copy of it.
from .startup import (ADAPTER_STARTUP_CEILING_SECONDS, ADAPTER_STARTUP_FLOOR_SECONDS,
                      ADAPTER_STARTUP_MEASURE_CEILING_SECONDS, ADAPTER_STARTUP_MULTIPLIER,
                      PROBE_ENDPOINT, adapter_startup_bound, measured_startup,
                      observed_startup, record_startup, startup_bound)

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
        # that did not complete never said the runtime was missing, and a
        # child that answered names its own answer.
        raise Denied("Container runtime failed: "
                     + probe_answer(versioned.exit_code, stop_note(versioned),
                                    versioned.stderr))
    version = versioned.stdout.strip().splitlines()[0] if versioned.stdout.strip() else "unknown"
    if config.sandbox.mode == "single":
        # Constrained-host path: Linux user namespaces only.
        prepared = ensure_single(config)
        evidence = run([*config.sandbox.namespace_helper, "sh", "-c",
                        "cat /proc/self/uid_map; id -u"], timeout=10, maximum=1024)
        probe_outcome(evidence.reason, evidence.exit_code,
                      stop_note(evidence) or evidence.stderr, "Namespace helper probe", 10)
        if evidence.exit_code != 0:
            # The helper that answered keeps its own answer, and the
            # message names it rather than ending with an empty detail.
            raise Denied("Namespace helper failed: "
                         + probe_answer(evidence.exit_code, stop_note(evidence),
                                        evidence.stderr))
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


#: Classify completed, refused and stopped one-shot adapter probes. Probe
#: timing is measured through process exit; managed runtimes measure their
#: initial protocol response instead, and the host record keeps the larger
#: per-engine interval.
def probe_outcome(reason, exit_code, detail, label: str, bound: int | None = None) -> None:
    """One classification for every probe the product runs.

    A probe that did not complete never reported its capabilities. The
    contract check and doctor share this classification. A timeout names the
    applied bound; an external signal kill names the signal. If the product
    chose a stop reason first, that reason stays primary and a termination
    signal is secondary detail.
    """
    if reason == "exited" and exit_code is not None and exit_code >= 0:
        return
    text = (detail or "").strip()
    if reason == "timeout" and bound:
        raise ConfigError(f"{label} exceeded its {bound} s bound (timeout) and was stopped "
                          "before it could report"
                          + (f": {text[-1000:]}" if text else ""))
    if reason in ("exited", "killed") and exit_code is not None and exit_code < 0:
        # A child a signal stopped never said what it holds, so its silence
        # is not a missing capability: the signal that stopped it is named
        # instead, so the operator is not sent to install the dependency.
        raise ConfigError(f"{label} was killed by signal {-exit_code} before it could report"
                          + (f": {text[-1000:]}" if text else ""))
    # The product decided this outcome itself and then stopped the child, so
    # the reason is the cause and the signal is only a fact about how the
    # child was stopped: one cause reads the same way on every host.
    stopped = f"; stopped by signal {-exit_code}" if exit_code is not None and exit_code < 0 else ""
    raise ConfigError(f"{label} did not complete ({reason}) and was stopped "
                      "before it could report" + stopped
                      + (f": {text[-1000:]}" if text else ""))


def probe_answer(exit_code, note: str, stderr: str) -> str:
    """The child's own answer when it answered and reported a failure.

    A child that completed is the only one that can have said what it
    holds, so its answer is its exit code, its stop fault and the text it
    produced. The capability message names that answer, so no message ends
    with an empty detail and the operator sees what the child said.

    The parts are named one by one, so every surface that probes the
    identical child formats the same answer the same way.
    """
    parts = []
    if exit_code:
        parts.append('exit %s' % exit_code)
    if note:
        parts.append(note)
    text = (stderr or '').strip()
    if text:
        parts.append(text[-1000:])
    return ': '.join(parts) if parts else 'the child reported a failure'


def probe_label(engine: str | None) -> str:
    """Return the shared engine-specific label for a one-shot probe."""
    if engine == "codex":
        return "Codex app-server stdio probe"
    if engine:
        return f"{engine} adapter contract probe"
    return "Adapter contract probe"


def adapter_probe(config, engine, command, *, maximum: int, label: str):
    """Measure a completed one-shot command, then run it under the bound.

    The measuring run spans process spawn through exit and is capped at 150 s.
    Any completed exit contributes a sample, including a nonzero exit; a
    measuring run that cannot complete leaves the 60 s floor in place. The
    shared record may also contain a runtime's spawn-to-first-response sample,
    so these are distinct endpoints and the larger interval supplies the
    conservative per-engine bound. A probe stopped before reporting is not
    evidence that a capability is missing.

    Returns the probe result, applied bound, published cost and the
    endpoint that measured it.
    """
    measured = run(command, timeout=ADAPTER_STARTUP_MEASURE_CEILING_SECONDS, maximum=maximum)
    if measured.reason == "exited" and measured.exit_code is not None and measured.exit_code >= 0:
        # This completed probe contributes its full process-exit interval.
        # The record retains the largest interval for this engine, including
        # any runtime first-response sample, and the reported pair follows
        # from that record.
        cost, bound, endpoint = observed_startup(config, engine, measured.seconds,
                                                 PROBE_ENDPOINT)
    else:
        # The measuring command did not complete within its ceiling. No
        # sample is recorded, so the probe uses the configured floor.
        bound = ADAPTER_STARTUP_FLOOR_SECONDS
        cost = None
        endpoint = None
    result = run(command, timeout=bound, maximum=maximum)
    probe_outcome(result.reason, result.exit_code,
                  stop_note(result) or result.stderr, label, bound)
    return result, bound, cost, endpoint


def startup_evidence(cost, bound: int, endpoint: str | None = None) -> dict:
    """This host's measured cost for one adapter child and the bound it needs.

    The cost is the one the bound was derived from, so the published pair
    satisfies the published rule: the bound is twelve times the cost, at
    least the floor and at most the ceiling. The endpoint names the
    measurement the cost came from, so the operator can tell a one-shot
    probe's spawn-to-exit interval from a managed runtime's
    spawn-to-first-answer interval. A cost that could not be measured is
    published as no cost at all: the bound is then the floor, because a
    child that has not answered cannot claim a wider one.
    """
    if cost is None:
        return {"startup_seconds": None,
                "host_relative_bound_seconds": bound,
                "startup_unmeasured": True}
    evidence = {"startup_seconds": round(float(cost), 3),
                "host_relative_bound_seconds": bound}
    if endpoint is not None:
        evidence["startup_endpoint"] = endpoint
    return evidence


def engine_contract(config: Config, engine: str) -> dict:
    """The adapter's own capability report, under this host's probe bound."""
    from .engine_config import adapter_contract
    contract = adapter_contract(engine)
    command = config.command(engine)
    if engine == "codex":
        result, bound, cost, endpoint = adapter_probe(
            config, engine, [*command, contract["command"][0], "--help"],
            maximum=262144, label=probe_label(engine))
        flags = list(contract["required_flags"])
        for item in contract.get("contracts", []):
            flags.extend(item["required_flags"])
        missing = [flag for flag in dict.fromkeys(flags) if not help_has_flag(result.stdout, flag)]
        if result.exit_code or missing:
            # A child that answered with a failure never declared which
            # flags it holds, so the message names that answer instead of
            # claiming that the flags are missing. A child that answered
            # successfully reported no failure of its own.
            answer = (probe_answer(result.exit_code, stop_note(result),
                                   result.stderr) if result.exit_code else "")
            detail = "" if result.exit_code else ", ".join(missing)
            raise ConfigError("Codex app-server stdio capability missing: "
                              + ": ".join(part for part in (detail, answer) if part))
        return {"request_unit": contract["request_unit"], "required_flags": sorted(set(flags)),
                "inference": "not_run", **startup_evidence(cost, bound, endpoint)}
    launcher = ROOT / "adapters" / engine / contract["entrypoint"]
    result, bound, cost, endpoint = adapter_probe(
        config, engine, [*command, str(launcher), "--check-contract"],
        maximum=8192, label=probe_label(engine))
    if result.exit_code:
        # The child answered and reported a failure, so its own answer is
        # named: the exit code, its stop fault and the text it produced.
        raise ConfigError("Required engine SDK capabilities missing: "
                          + probe_answer(result.exit_code, stop_note(result),
                                         result.stderr))
    try:
        reported = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigError(f"Unreadable {engine} contract report: {exc}") from exc
    verified = reported.get("exports", []) if isinstance(reported, dict) else []
    missing = [name for name in contract["exports"] if name not in verified]
    if missing:
        raise ConfigError(f"Adapter drift: {engine} contract declares missing exports: " + ", ".join(missing))
    return {"request_unit": contract["request_unit"], "exports": sorted(set(verified)),
            "inference": "not_run", **startup_evidence(cost, bound, endpoint)}


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


def node_capability(config: Config) -> str:
    """The Node module and stream capabilities the SDK needs, from the child.

    The child that answers decides its own outcome: a child a signal
    stopped never said a capability was missing, and a child that merely
    outlived its bound never answered at all, so the shared classification
    names that outcome instead of a missing dependency. The two remedies
    stay distinguishable: provision Node, or give this host more room.

    The probe is a module-level check of its own, like the container
    runtime and the sandbox image, so every capability message the doctor
    publishes is reachable and can be regression-tested on its own.
    """
    result = run([*config.command('pi'), "--input-type=module", "-e",
                  "import {createRequire} from 'node:module'; import {ReadableStream} from 'node:stream/web'; if(typeof createRequire!=='function'||typeof ReadableStream!=='function')process.exit(1); console.log('module and stream capabilities available')"], timeout=10, maximum=1024)
    probe_outcome(result.reason, result.exit_code,
                  stop_note(result) or result.stderr, "Node capability probe", 10)
    if result.exit_code:
        # The child answered and reported a failure: name the failure it
        # reported, not a capability it never claimed to be missing.
        raise ConfigError('Required Node module/stream capabilities missing: '
                          + probe_answer(result.exit_code, stop_note(result),
                                         result.stderr))
    return result.stdout.strip()


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
    checks.append(check_runner_row(config))
    checked("proposal stores", lambda: proposal_stores(config))
    engines = {config.engine(p) for p in config.profiles}
    if "pi" in engines or "pi-durable" in engines:
        checked("node", lambda: node_capability(config))
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


#: Bounded project-settings read for the check-runner diagnostic. The doctor
#: reads untrusted local files, so the settings stay under the same bound the
#: project lifecycle enforces; anything unreadable or malformed fails closed.
_CHECK_RUNNER_SETTINGS_MAXIMUM = 65536


def _check_job_projects(config: Config) -> list[str]:
    """Initialized projects with an active role granting ``check_job``.

    Schema/bounds: ``config.data/projects/*/project.toml`` under a 64 KiB
    bound. Trust: local project settings plus configured roles. Retry: none.
    Evidence: project names. Failure: ConfigError names the project whose
    settings are unreadable, malformed or refer to unknown roles, so a broken
    project is never silently read as having no grant.
    """
    projects_root = config.data / "projects"
    try:
        with os.scandir(projects_root) as scan:
            entries = sorted((entry for entry in scan if not entry.name.startswith(".")),
                             key=lambda entry: entry.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConfigError(f"Cannot list initialized projects: {exc}") from exc
    needing: list[str] = []
    from .project import check_verify
    from .selection import attributes

    for candidate in entries:
        try:
            if candidate.is_symlink():
                raise ConfigError(f"{candidate.name}: project directory must not be a symlink")
            if not candidate.is_dir(follow_symlinks=False):
                continue
        except OSError as exc:
            raise ConfigError(f"{candidate.name}: cannot inspect project directory: {exc}") from exc
        entry = Path(candidate.path)
        settings_path = entry / "project.toml"
        try:
            settings_path.lstat()
        except FileNotFoundError:
            # A directory without project.toml is not an initialized project.
            continue
        except OSError as exc:
            raise ConfigError(f"{entry.name}: cannot stat project settings: {exc}") from exc
        try:
            raw = safe_read(entry, "project.toml", _CHECK_RUNNER_SETTINGS_MAXIMUM)
        except (OSError, ValueError, Denied, LimitExceeded) as exc:
            raise ConfigError(f"{entry.name}: cannot read bounded project settings: {exc}") from exc
        try:
            settings = tomllib.loads(raw.decode("utf-8"))
            keys(settings, {"roles", "verify", "attributes"}, "project")
            roles = strings(settings.get("roles", []), "project.roles")
            if not roles:
                raise ConfigError("Project roles must refer to configured roles")
            check_verify(settings.get("verify", []))
            attributes(settings.get("attributes", {}))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError,
                MizuError, TypeError) as exc:
            raise ConfigError(f"{entry.name}: malformed project settings: {exc}") from exc
        unknown = [role for role in roles if role not in config.roles]
        if unknown:
            raise ConfigError(f"{entry.name}: unknown configured role: " + ", ".join(sorted(unknown)))
        try:
            name = identifier(entry.name)
        except Denied as exc:
            raise ConfigError(f"{entry.name}: invalid project name: {exc}") from exc
        if any("check_job" in config.roles[role].capabilities for role in roles):
            needing.append(name)
    return sorted(needing)


def check_runner(config: Config, directory: Path | None = None, *, system: str | None = None) -> dict:
    """Check-job runner presence for projects that grant ``check_job``.

    No service manager is contacted and neither grants nor generated units
    change here; this only inspects initialized projects and the standard
    platform service directory. On Linux every granted project needs its full
    generated runner set (check ``.service``, notification ``.path`` and
    recovery ``.timer``); a missing file fails with the exact unit names.
    On macOS/Windows the runner is operator-managed and generated service
    definitions are absent by design (``docs/check-jobs.md``), so the result
    carries ``state: manual`` and the caller reports ``not_run`` instead of
    passing or failing on generated units. With no grant the result carries
    ``state: no_grant``.
    """
    from .services import check_runner_units

    name = system or _platform.SYSTEM
    if name not in ("linux", "macos", "windows"):
        raise ConfigError(f"Unsupported service platform: {name}")
    needing = _check_job_projects(config)
    if not needing:
        return {"state": "no_grant", "projects": [],
                "detail": "No initialized project grants check_job; no check-job runner is required."}
    if name in ("macos", "windows"):
        return {"state": "manual", "system": name, "projects": needing,
                "detail": ("Check-job runner is operator-managed on " + name + "; "
                           "generated service definitions are absent by design "
                           "(docs/check-jobs.md). Operate `mizu check-jobs PROJECT` "
                           "from an operator-managed runner; doctor cannot verify it.")}
    service_dir = directory or _platform.service_dir(name)
    missing: list[str] = []
    installed: list[str] = []
    for project in needing:
        for unit in check_runner_units(project):
            try:
                present = (service_dir / unit).is_file()
            except OSError as exc:
                raise ConfigError(f"Cannot inspect service directory {service_dir}: {exc}") from exc
            (installed if present else missing).append(unit)
    if missing:
        raise ConfigError(
            "Missing check-job runner units in " + str(service_dir) + ": "
            + ", ".join(sorted(missing))
            + "; install and enable them before check_job use (docs/check-jobs.md).")
    return {"state": "ready", "system": name, "projects": needing,
            "directory": str(service_dir), "units": sorted(installed)}


def check_runner_row(config: Config, directory: Path | None = None, *,
                     system: str | None = None) -> dict:
    """One doctor row for the check-job runner diagnostic.

    ``pass`` carries the installed Linux definitions, ``fail`` names the
    exact missing unit files (or the malformed project that fails closed),
    and ``not_run`` carries the explicit no-grant or manual-runner state.
    """
    try:
        details = check_runner(config, directory, system=system)
    except (MizuError, OSError, ValueError, TypeError, KeyError) as exc:
        return {"name": "check-job runner", "status": "fail", "details": str(exc)}
    if details.get("state") in ("no_grant", "manual"):
        return {"name": "check-job runner", "status": "not_run", "details": details}
    return {"name": "check-job runner", "status": "pass", "details": details}


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
