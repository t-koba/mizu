#!/usr/bin/env python3
"""Stage immutable releases and promote only at an operator-controlled boundary."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mizu import NODE_MINIMUM, __version__
from mizu import platform as _platform
from mizu.cli import configure
from mizu.distribution import source_files as inventory
from mizu.config import load
from mizu.errors import Denied, MizuError
from mizu.fs import canonical, digest, sync_dir, lock, mkdir, now, read_json, write_json




def source_files():
    yield from inventory(ROOT)


def source_manifest(root=ROOT):
    return {relative.as_posix(): {"sha256": digest(path.read_bytes()), "executable": bool(path.stat().st_mode & 0o111)}
            for relative, path in inventory(root)}


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
                scripts = [] if args.allow_install_scripts else ["--ignore-scripts"]
                if not (adapter / "package-lock.json").exists():
                    command(["npm", "install", "--package-lock-only", *common, "--ignore-scripts"], cwd=adapter)
                command(["npm", "ci", *common, *scripts], cwd=adapter)
                pi = adapter / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
                result = subprocess.check_output([node, pi, "--version"], text=True, timeout=30).strip()
                command([node, str(adapter / "launcher.mjs"), "--check-contract"], cwd=adapter)
                pi_lock = digest((adapter / "package-lock.json").read_bytes())
            # Offline source tests are a release gate. They do not claim live Pi/Podman validation.
            command([sys.executable, "scripts/check.py", "--report", "installation-checks.json"], cwd=temporary, timeout=240)
            checks = read_json(temporary / "installation-checks.json")
            write_json(temporary / "source-manifest.json", manifest)
            write_json(temporary / "installation.json", {"version": __version__, "source_sha256": sha,
                        "installed_at": now(), "mode": "core-only" if args.core_only else "pi",
                        "pi_version": None if args.core_only else result, "pi_lock_sha256": pi_lock,
                        "node": node, "install_scripts_enabled": args.allow_install_scripts,
                        "validation": "local-install-checks-passed", "checks": checks["checks"], "live_validation": "not_run"})
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
    p.add_argument("--allow-install-scripts", action="store_true", help="Explicitly allow dependency lifecycle scripts after reviewing the lock")
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
