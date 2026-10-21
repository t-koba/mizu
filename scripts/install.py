#!/usr/bin/env python3
"""Stage immutable releases and promote only at an operator-controlled boundary."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mizu import NODE_MINIMUM, __version__
from mizu import platform as _platform
from mizu.cli import configure
from mizu.distribution import OFFLINE_REPORT_VERSION, source_files as inventory
from mizu.distribution import source_manifest as distribution_manifest
from mizu.config import load
from mizu.errors import Denied, MizuError
from mizu.fs import canonical, digest, sync_dir, lock, mkdir, now, read_json, write_json




def source_files():
    yield from inventory(ROOT)


def source_manifest(root=ROOT):
    return distribution_manifest(root)


def staging_not_run_checks(*, core_only: bool) -> str:
    """Return only the SDK gates unavailable in each installer staging mode.

    Both modes omit pi-durable. Core-only also omits the Pi adapter; full Pi
    staging provisions it and must run its SDK contract gate unwaived.
    """
    checks = ["node-durable-contract-tests", "node-pi-reasoning-replay-tests"]
    if core_only:
        checks.insert(1, "node-pi-sdk-contract-tests")
    return ",".join(checks)


def functional_bash_available() -> bool:
    """Match the check driver's POSIX Bash probe before reusing its evidence."""
    if os.name != "posix":
        return False
    bash = shutil.which("bash")
    if not bash:
        return False
    try:
        probe = subprocess.run([bash, "-n"], input=b"exit 0\n", capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def current_node_version() -> str | None:
    """Return the exact Node runtime used to compare source-gate evidence."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        version = subprocess.check_output([node, "--version"], text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return version or None


#: Gates that must pass in any reused offline source report.
REQUIRED_REUSED_GATE_NAMES = frozenset({"python-syntax", "python-unit-and-contract-tests",
                                       "node-transport-and-extension-tests", "source-manifest-stable"})

#: SDK gates that may appear as not_run; reuse binds to the exact core-only set.
SDK_WAIVABLE_GATE_NAMES = frozenset({"node-durable-contract-tests", "node-pi-sdk-contract-tests",
                                     "node-pi-reasoning-replay-tests"})


def validate_reused_source_checks(report, *, expected_sha: str):
    """Validate an explicit prior source-gate report for core-only reuse.

    The report must be the versioned offline receipt produced by
    scripts/check.py on this exact source tree, interpreter, and platform,
    with ok true, complete required results, and no waivers outside the core-only
    staging waiver set. Every not_run result must be explicitly waived. Anything else raises Denied; the caller still keeps
    the mandatory staged-manifest equality check.
    """
    if not isinstance(report, dict):
        raise Denied("Reused source report is malformed: expected a JSON object")
    if report.get("report_version") != OFFLINE_REPORT_VERSION:
        raise Denied(f"Reused source report has an unsupported schema version: {report.get('report_version')!r}")
    if report.get("suite") != "offline":
        raise Denied("Reused source report is malformed: expected suite 'offline'")
    if report.get("mizu_version") != __version__:
        raise Denied("Reused source report is stale: mizu version mismatch")
    if report.get("source_sha256") != expected_sha:
        raise Denied("Reused source report is stale: source manifest digest mismatch")
    if report.get("python") != sys.version.split()[0]:
        raise Denied("Reused source report is for a different Python version")
    if report.get("os") != sys.platform:
        raise Denied("Reused source report is for a different operating system")
    if report.get("node_version") != current_node_version() or not report.get("node_version"):
        raise Denied("Reused source report is for a different or unavailable Node runtime")
    if report.get("ok") is not True:
        raise Denied("Reused source report did not pass its gate")
    entries = report.get("checks")
    if not isinstance(entries, list) or not entries:
        raise Denied("Reused source report is incomplete: missing check results")
    expected_waivers = set(staging_not_run_checks(core_only=True).split(","))
    declared_waivers = report.get("allowed_not_run")
    if (not isinstance(declared_waivers, list)
            or any(not isinstance(name, str) for name in declared_waivers)
            or declared_waivers != sorted(set(declared_waivers))
            or not set(declared_waivers) <= expected_waivers):
        raise Denied("Reused source report has an unexpected allowed_not_run set")
    if not isinstance(report.get("bash_available"), bool):
        raise Denied("Reused source report is missing its functional Bash status")
    if report["bash_available"] != functional_bash_available():
        raise Denied("Reused source report is for a different functional Bash environment")
    by_name = {}
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or entry.get("status") not in ("pass", "not_run")):
            raise Denied("Reused source report contains an invalid or failed check")
        name, status = entry["name"], entry["status"]
        if name in by_name:
            raise Denied(f"Reused source report contains a duplicate check: {name}")
        by_name[name] = status
        if status == "not_run" and name not in declared_waivers:
            if name != "shell-syntax" or report["bash_available"]:
                raise Denied(f"Reused source report contains an unsupported not_run check: {name}")
    required = set(REQUIRED_REUSED_GATE_NAMES)
    # Match check.py's syntax inventory for this same digest-bound tree.
    for directory in ("adapters/pi", "adapters/pi-durable", "scripts"):
        required.update("javascript-syntax:" + path.relative_to(ROOT).as_posix() for path in (ROOT / directory).glob("*.mjs"))
    if report["bash_available"]:
        required.update("shell-syntax:" + path.name for path in (ROOT / "scripts").glob("*.sh"))
    elif by_name.get("shell-syntax") != "not_run":
        raise Denied("Reused source report is missing its no-Bash shell-syntax result")
    for name in sorted(required):
        if by_name.get(name) != "pass":
            raise Denied(f"Reused source report is incomplete: {name} did not pass")
    for name in sorted(SDK_WAIVABLE_GATE_NAMES):
        if by_name.get(name) not in ("pass", "not_run"):
            raise Denied(f"Reused source report is incomplete: missing SDK gate {name}")
    return entries


def verify_release(release: Path):
    receipt = read_json(release / "installation.json")
    manifest = read_json(release / "source-manifest.json")
    if digest(canonical(manifest)) != receipt.get("source_sha256"):
        raise Denied("Release source manifest does not match its installation receipt")
    for name, expected in manifest.items():
        relative = Path(name)
        if relative.is_absolute() or any(part in (".", "..") for part in relative.parts):
            raise Denied("Unsafe source manifest path")
        path = release / relative
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != release.parent):
            raise Denied("Release source must not contain symlinks")
        if (not path.is_file() or digest(path.read_bytes()) != expected["sha256"]
                or bool(path.stat().st_mode & 0o111) != expected["executable"]):
            raise Denied("Release source has changed after validation: " + name)
    if receipt.get("pi_lock_sha256"):
        if digest((release / "adapters/pi/package-lock.json").read_bytes()) != receipt["pi_lock_sha256"]:
            raise Denied("Installed dependency lock changed after validation")


def command(argv, *, cwd=None, timeout=600):
    subprocess.run(list(map(str, argv)), cwd=cwd, check=True, timeout=timeout, stdin=subprocess.DEVNULL, stdout=sys.stderr)


@contextlib.contextmanager
def stopped(config_file: Path):
    with contextlib.ExitStack() as stack:
        if config_file.exists():
            config = load(config_file)
            for path in sorted((config.data / "locks").glob("daemon-*.lock")):
                stack.enter_context(lock(path, blocking=False))
            for project in sorted((config.data / "projects").glob("*")):
                if not project.is_dir() or project.name.startswith("."):
                    continue
                control = read_json(project / "control.json", {})
                if control.get("armed") and not control.get("paused"):
                    raise Denied("Pause all projects and stop their services before promoting a release")
                for path in sorted((project / "locks").glob("*.lock")):
                    stack.enter_context(lock(path, blocking=False))
            for path in sorted((config.data / "slots").glob("*.lock")):
                stack.enter_context(lock(path, blocking=False))
            # Locks serialize mizu processes; service state is a best-effort second check.
            for unit in active_service_units():
                raise Denied(f"Stop service {unit} before promoting a release")
        yield


def active_service_units() -> list[str]:
    """Running mizu-* services on this platform, or [] when unavailable."""
    system = _platform.SYSTEM
    try:
        if system == "macos":
            result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10,
                                    stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                return []
            return [line.split()[-1] for line in result.stdout.splitlines()
                    if line.split() and line.split()[-1].startswith("mizu-")]
        if system == "windows":
            result = subprocess.run(["schtasks", "/Query", "/FO", "LIST"], capture_output=True, text=True,
                                    timeout=15, stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                return []
            running, name = [], None
            for line in result.stdout.splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip().lower(), value.strip()
                if key == "taskname":
                    name = value.lstrip("\\")
                elif key == "status" and name and name.startswith("mizu-"):
                    if value.lower() == "running":
                        running.append(name)
                    name = None
            return running
        result = subprocess.run(["systemctl", "--user", "list-units", "mizu-*", "--state=running",
                                 "--no-legend", "--no-pager"], capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.split()[0] for line in result.stdout.splitlines() if line.split()]


def recover_link(destination: Path):
    """Recover a managed link interrupted in the Windows replacement gap.

    A live destination wins. Reserved recovery links belong to this installer;
    a regular destination/recovery file is never removed automatically.
    """
    recovery=destination.parent/('.'+destination.name+'.recovery')
    if not recovery.is_symlink():
        if recovery.exists():raise Denied('Recovery record must be a managed symlink')
        return
    if destination.is_symlink():
        recovery.unlink()
    elif destination.exists():
        raise Denied('Refusing recovery over an unrelated regular file')
    else:
        os.replace(recovery,destination)
    sync_dir(destination.parent)


def link_atomically(destination: Path, target: Path):
    recover_link(destination)
    temporary = destination.parent / ("." + destination.name + ".next")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        raise Denied(f"Cannot create symlink {destination} (on Windows enable Developer Mode or run with symlink privilege): {exc}") from exc
    try:
        os.replace(temporary, destination)
    except OSError:
        # Windows cannot rename over an existing directory symlink. POSIX
        # keeps the atomic path above; there the fallback never triggers.
        if not _platform.IS_WINDOWS or not destination.is_symlink():
            raise
        old_target = os.readlink(destination)
        backup = destination.parent / ("." + destination.name + ".recovery")
        backup.unlink(missing_ok=True)
        backup.symlink_to(old_target, target_is_directory=destination.is_dir())
        destination.unlink()
        try:
            os.replace(temporary, destination)
        except OSError:
            os.replace(backup, destination)
            raise
        backup.unlink(missing_ok=True)


def promote(args, release: Path):
    release = release.resolve()
    # Compare resolved paths: TMPDIR and similar prefixes routinely contain
    # symlinks (macOS /var -> /private/var), and an unresolved prefix would
    # falsely fail the related-command check below.
    prefix = args.prefix.resolve()
    if release.parent != (args.prefix / "releases").resolve() or not (release / "installation.json").is_file():
        raise Denied("Only a staged release in this prefix can be promoted")
    if read_json(release / "installation.json").get("validation") != "local-install-checks-passed":
        raise Denied("The candidate has not passed installation checks")
    verify_release(release)
    command([sys.executable, release / "bin/mizu", "--version"], timeout=15)
    launcher = args.bin_dir / "mizu"
    if launcher.exists() or launcher.is_symlink():
        if not launcher.is_symlink() or prefix not in launcher.resolve().parents:
            raise Denied("Refusing to replace an unrelated mizu command")
    with stopped(args.config):
        current = args.prefix / "current"
        recover_link(current)
        old = current.resolve() if current.is_symlink() else None
        if current.exists() and not current.is_symlink():
            raise Denied("current must be a managed symlink")
        old_mode = read_json(old / "installation.json", {}).get("mode") if old else None
        new_mode = read_json(release / "installation.json", {}).get("mode")
        if old_mode and old_mode != new_mode and not getattr(args, "allow_mode_change", False):
            raise Denied(f"Release mode changes from {old_mode} to {new_mode}; "
                         "re-run with --allow-mode-change after review")
        link_atomically(current, release)
        mkdir(args.bin_dir)
        launcher = args.bin_dir / "mizu"
        if launcher.exists() or launcher.is_symlink():
            if not launcher.is_symlink() or prefix not in launcher.resolve().parents:
                raise Denied("Refusing to replace an unrelated mizu command")
        link_atomically(launcher, current / "bin/mizu")
        if old and old != release and old.parent == release.parent and not old.is_symlink():
            shutil.rmtree(old)
    result = {"status": "promoted", "release": str(release),
              "note": "Services remain stopped. Run doctor/smoke, then explicitly start and arm. "
                      "Only code was switched; data, side effects and budgets are unchanged."}
    if old_mode and old_mode != new_mode:
        result["mode_change"] = {"from": old_mode, "to": new_mode}
        result["note"] += " Agent runtime availability changed; re-run doctor and smoke before arming."
    return result


def stage(args):
    if sys.version_info < (3, 11):
        raise Denied("Python >=3.11 is required")
    if _platform.is_root():
        raise Denied("Run setup as a normal user. The separate system-deps script is the only privileged step")
    if getattr(args, "reuse_source_checks", None) is not None and not args.core_only:
        raise Denied("Source-check reuse is only allowed with --core-only")
    node = shutil.which("node")
    if not args.core_only:
        if not node or not shutil.which("npm"):
            raise Denied(f"Install Node >={NODE_MINIMUM} and npm first; see scripts/install-node.sh")
        version = subprocess.check_output([node, "-p", "process.versions.node"], text=True, timeout=10).strip()
        if tuple(map(int, version.split("."))) < tuple(map(int, NODE_MINIMUM.split("."))):
            raise Denied(f"The pinned Pi requires Node >={NODE_MINIMUM}; the existing Node was not changed")
    manifest = source_manifest()
    sha = digest(canonical(manifest))
    name = f"{__version__}-{sha[:12]}-" + ("core" if args.core_only else "pi")
    release = args.prefix / "releases" / name
    if release.exists():
        receipt = read_json(release / "installation.json", {})
        if receipt.get("source_sha256") != sha:
            raise Denied("Existing release fingerprint differs")
    else:
        mkdir(release.parent)
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=release.parent))
        try:
            for relative, source in source_files():
                target = temporary / relative
                mkdir(target.parent)
                shutil.copy2(source, target)
            staged_manifest = source_manifest(temporary)
            if staged_manifest != manifest:
                raise Denied("Source changed while staging; retry from a stable candidate copy")
            pi_lock = None
            if not args.core_only:
                adapter = temporary / "adapters/pi"
                common = ["--engine-strict", "--no-audit", "--no-fund"]
                # Permit npm's own script policy when opted in; on npm v12 removing
                # --ignore-scripts still defers to allowScripts default-deny, so the flag
                # is requested permission, not proof scripts ran. Default stays deny-all.
                scripts = [] if args.allow_install_scripts else ["--ignore-scripts"]
                if not (adapter / "package-lock.json").exists():
                    command(["npm", "install", "--package-lock-only", *common, "--ignore-scripts"], cwd=adapter)
                command(["npm", "ci", *common, *scripts], cwd=adapter)
                pi = adapter / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
                result = subprocess.check_output([node, pi, "--version"], text=True, timeout=30).strip()
                command([node, str(adapter / "launcher.mjs"), "--check-contract"], cwd=adapter)
                pi_lock = digest((adapter / "package-lock.json").read_bytes())
            reuse_report = getattr(args, "reuse_source_checks", None)
            if reuse_report is not None:
                # Explicit, content-bound reuse for core-only staging only.
                # The staged-manifest equality check above stays mandatory;
                # without an explicit receipt the full staged gate below runs.
                if not args.core_only:
                    raise Denied("Source-check reuse is only allowed with --core-only")
                try:
                    report_path = Path(reuse_report)
                    if report_path.is_symlink():
                        raise Denied("Reused source report must not be a symlink")
                    report_stat = report_path.stat()
                    if not stat.S_ISREG(report_stat.st_mode) or report_stat.st_size > 1024 * 1024:
                        raise Denied("Reused source report must be a regular file of at most 1 MiB")
                    with report_path.open("rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise Denied("Reused source report must be a regular file")
                        data = stream.read(1024 * 1024 + 1)
                    if len(data) > 1024 * 1024:
                        raise Denied("Reused source report exceeds 1 MiB")
                    reused = json.loads(data)
                except (OSError, ValueError, RecursionError) as exc:
                    raise Denied(f"Reused source report is unreadable: {exc}") from exc
                check_entries = validate_reused_source_checks(reused, expected_sha=sha)
                checks_provenance = "reused-source-gate"
            else:
                # Offline source tests are a release gate. They do not claim live Pi/Podman validation.
                # Staging never provisions pi-durable, and core-only staging has no Pi adapter;
                # record only those unavailable SDK suites as not_run. Full Pi staging must run
                # the Pi SDK contract gate against the adapter it just provisioned.
                command([sys.executable, "scripts/check.py", "--report", "installation-checks.json",
                         "--allow-not-run", staging_not_run_checks(core_only=args.core_only)],
                        # The staging call holds the gate's modelled path (2646 s on
                        # Windows, the largest), and the core CI cell that installs
                        # holds this bound together with the source gate's own path.
                        cwd=temporary, timeout=2700)
                staged_report = read_json(temporary / "installation-checks.json")
                check_entries = staged_report["checks"]
                checks_provenance = "staged-gate"
            write_json(temporary / "source-manifest.json", manifest)
            write_json(temporary / "installation.json", {"version": __version__, "source_sha256": sha,
                        "installed_at": now(), "mode": "core-only" if args.core_only else "pi",
                        "pi_version": None if args.core_only else result, "pi_lock_sha256": pi_lock,
                        "node": node, "install_scripts_enabled": args.allow_install_scripts,  # requested permission, not proof scripts ran (npm v12 may still deny via allowScripts),
                        "validation": "local-install-checks-passed", "checks": check_entries,
                        "checks_provenance": checks_provenance, "live_validation": "not_run"})
            os.rename(temporary, release)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    current = args.prefix / "current"
    activate = args.activate or (not current.exists() and not args.stage_only)
    result = promote(args, release) if activate else {"status": "staged", "release": str(release)}
    if not args.config.exists() and activate:
        pi_command = None
        if not args.core_only:
            pi_command = json.dumps([node])
        result["configuration"] = configure(args.config, pi_command)
    result["mode"] = "core-only; no agent runtime installed" if args.core_only else "Pi installed; live validation still required"
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/mizu", help="Release storage prefix")
    p.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin", help="Directory for the mizu launcher link")
    p.add_argument("--config", type=Path, default=_platform.default_config_file(), help="Private config file to create on activation")
    p.add_argument("--core-only", action="store_true", help="Offline core installation, without Pi or permission to run agents")
    p.add_argument("--reuse-source-checks", type=Path, default=None,
                   help="Reuse an explicit prior offline source-gate report (core-only only); "
                        "the report must bind to this exact source manifest, Python, OS, schema, "
                        "passing result, and only explicitly declared core-only waivers")
    p.add_argument("--allow-install-scripts", action="store_true", help="Permit dependency lifecycle scripts under npm's own policy after reviewing the lock")
    p.add_argument("--allow-mode-change", action="store_true", help="Permit promotion across core-only/pi modes after review")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--stage-only", action="store_true", help="Stage a validated release without promoting it")
    group.add_argument("--activate", action="store_true", help="Promote after validation; existing services must be stopped")
    group.add_argument("--promote", metavar="RELEASE_DIRECTORY", type=Path, help="Promote an already-staged release directory")
    args = p.parse_args()
    for name in ("prefix", "bin_dir", "config"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    if any(c in str(args.prefix) for c in ("\n", "\r")):
        p.error("Installation paths cannot contain newlines")
    try:
        mkdir(args.prefix)
        with lock(args.prefix / ".install.lock", blocking=False):
            if args.promote:
                result = promote(args, args.promote)
            else:
                result = stage(args)
        print(json.dumps(result, indent=2))
        return 0
    except (MizuError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return getattr(exc, "code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
