#!/usr/bin/env python3
"""Stage immutable releases and promote only at an operator-controlled boundary."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mizu import NODE_MINIMUM, __version__
from mizu import install_migration, platform as _platform
from mizu.cli import configure
from mizu.distribution import source_files as inventory
from mizu.distribution import source_manifest as distribution_manifest
from mizu.config import load
from mizu.errors import Busy, Denied, MizuError
from mizu.fs import canonical, digest, sync_dir, lock, mkdir, now, read_json, reuse_owned_locks, write_json


def source_files():
    yield from inventory(ROOT)


def source_manifest(root=ROOT):
    return distribution_manifest(root)


class StagingInterrupted(BaseException):
    """Raised by a staging signal handler after it removes its temp directory."""

    def __init__(self, signum):
        self.signum = signum
        super().__init__(f"Staging interrupted by signal {signum}")


#: Bound on abandoned staging directories cleaned per staging run.
STAGE_REAP_LIMIT = 64


def _stage_owner_lock(directory: Path) -> Path:
    """Owner-lock path for a staging directory, kept outside the staged tree."""
    return directory.parent / ".owner-locks" / (directory.name + ".lock")


def _clean_staging_dir(path: Path) -> None:
    """Remove only a real .stage-* directory; never follow symlinks or raise."""
    try:
        if path.name.startswith(".stage-") and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    except OSError:
        pass


@contextlib.contextmanager
def stage_ownership(directory: Path):
    """Hold a kernel lock naming this process as a live stager of directory."""
    lock_path = _stage_owner_lock(directory)
    with lock(lock_path, blocking=False):
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()


@contextlib.contextmanager
def stage_signal_cleanup(directory: Path):
    """Remove directory on SIGTERM/SIGINT/SIGHUP, then unwind staging.

    Yields the installed handler so tests can simulate a signal in-process.
    Handlers are restored on normal exit; unsupported signals are skipped.
    """
    watched = [sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None),
                               getattr(signal, "SIGHUP", None)) if sig is not None]

    def handle(signum, _frame):
        _clean_staging_dir(directory)
        raise StagingInterrupted(signum)

    previous = {}
    try:
        for sig in watched:
            try:
                previous[sig] = signal.signal(sig, handle)
            except (ValueError, OSError):
                pass  # non-main thread or unsupported signal
        yield handle
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


def reap_abandoned_staging(release_parent: Path, *, limit: int = STAGE_REAP_LIMIT) -> list:
    """Remove .stage-* directories with no live owner; return their names.

    A live stager holds a nonblocking kernel lock on its owner-lock path;
    acquiring that lock means no live process owns the directory. Each
    candidate is bounded and symlinks are never followed. Reaping runs under
    the prefix install lock, so no second stager can race in this prefix.
    """
    reaped = []
    for path in sorted(release_parent.glob(".stage-*"))[:limit]:
        if path.is_symlink() or not path.is_dir():
            continue
        lock_path = _stage_owner_lock(path)
        try:
            with lock(lock_path, blocking=False):
                if path.is_symlink() or not path.is_dir():
                    continue
                shutil.rmtree(path)
                reaped.append(path.name)
            with contextlib.suppress(OSError):
                lock_path.unlink()
        except (Busy, OSError):
            continue  # live owner or unsafe candidate: leave it untouched
    return reaped


def verify_release(release: Path, *, expected_manifest=None):
    receipt = read_json(release / "installation.json")
    manifest = read_json(release / "source-manifest.json")
    if not isinstance(receipt, dict) or not isinstance(manifest, dict):
        raise Denied("Release installation metadata is malformed")
    if expected_manifest is not None and manifest != expected_manifest:
        raise Denied("Release source manifest differs from the current source")
    if digest(canonical(manifest)) != receipt.get("source_sha256"):
        raise Denied("Release source manifest does not match its installation receipt")
    try:
        actual_manifest = source_manifest(release)
    except (OSError, ValueError) as exc:
        raise Denied(f"Release source inventory is invalid: {exc}") from exc
    if actual_manifest != manifest:
        raise Denied("Release source files do not match its installation manifest")
    if receipt.get("pi_lock_sha256"):
        if digest((release / "adapters/pi/package-lock.json").read_bytes()) != receipt["pi_lock_sha256"]:
            raise Denied("Installed Pi dependency lock changed after validation")
    if receipt.get("pi_durable_lock_sha256"):
        if digest((release / "adapters/pi-durable/package-lock.json").read_bytes()) != receipt["pi_durable_lock_sha256"]:
            raise Denied("Installed pi-durable dependency lock changed after validation")
    return manifest


def command(argv, *, cwd=None, timeout=600):
    subprocess.run(list(map(str, argv)), cwd=cwd, check=True, timeout=timeout, stdin=subprocess.DEVNULL, stdout=sys.stderr)


@contextlib.contextmanager
def stopped(config_file: Path):
    with contextlib.ExitStack() as stack:
        config, projects, project_locks = None, [], []
        if config_file.exists():
            config = load(config_file)
            for path in sorted((config.data / "locks").glob("daemon-*.lock")):
                stack.enter_context(lock(path, blocking=False))
            projects = install_migration.managed_projects(config)
            for project in projects:
                control = read_json(project / "control.json", {})
                if control.get("armed") and not control.get("paused"):
                    raise Denied("Pause all projects and stop their services before promoting a release")
                for path in sorted((project / "locks").glob("*.lock")):
                    stack.enter_context(lock(path, blocking=False))
                    project_locks.append(path)
            for path in sorted((config.data / "slots").glob("*.lock")):
                stack.enter_context(lock(path, blocking=False))
            # Locks serialize mizu processes; service state is a best-effort second check.
            for unit in active_service_units():
                raise Denied(f"Stop service {unit} before promoting a release")
        # Storage and explicit converters take their usual locks; borrow only
        # the overlapping exclusions already acquired by this thread.
        with reuse_owned_locks(project_locks):
            yield config, projects


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
    manifest = verify_release(release)
    command([sys.executable, release / "bin/mizu", "--version"], timeout=15)
    launcher = args.bin_dir / "mizu"
    if launcher.exists() or launcher.is_symlink():
        if not launcher.is_symlink() or prefix not in launcher.resolve().parents:
            raise Denied("Refusing to replace an unrelated mizu command")
    with stopped(args.config) as project_state:
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
        # An older staged release must not trigger this release's conversions.
        migrations = (install_migration.convert(*project_state, release)
                      if "src/mizu/install_migration.py" in manifest else [])
        try:
            link_atomically(current, release)
            mkdir(args.bin_dir)
            launcher = args.bin_dir / "mizu"
            if launcher.exists() or launcher.is_symlink():
                if not launcher.is_symlink() or prefix not in launcher.resolve().parents:
                    raise Denied("Refusing to replace an unrelated mizu command")
            link_atomically(launcher, current / "bin/mizu")
            if old and old != release and old.parent == release.parent and not old.is_symlink():
                shutil.rmtree(old)
        except (MizuError, OSError) as exc:
            if not migrations:
                raise
            raise Denied(f"Promotion failed after project data conversion: {exc}. "
                         f"Inspect the current/launcher links and conversion receipts in "
                         f"{Path(migrations[0]['archive']).parent} before resuming.") from exc
    change_note = ("Listed migrations were applied after verified backups; "
                   "see migrations for archives and receipts. Budgets are unchanged."
                   if migrations else "Only code was switched; data, side effects and budgets are unchanged.")
    result = {
        "status": "promoted", "release": str(release), "migrations": migrations,
        "uncovered_families": {
            "insight-identities-and-tombstones": (
                "Promotion does not convert this family; the runtime still reads and writes insight-ids/."),
            "decision-receipts": (
                "Promotion does not assess or convert this family; reconciliation is a separate route-bound host operation."),
        },
        "note": "Services remain stopped. Run doctor/smoke, then explicitly start and arm. " + change_note,
    }
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
        if release.is_symlink():
            raise Denied("Existing release must not be a symlink")
        verify_release(release.resolve(), expected_manifest=manifest)
    else:
        mkdir(release.parent)
        reap_abandoned_staging(release.parent)
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=release.parent))
        try:
            with stage_ownership(temporary), stage_signal_cleanup(temporary):
                for relative, source in source_files():
                    target = temporary / relative
                    mkdir(target.parent)
                    shutil.copy2(source, target)
                staged_manifest = source_manifest(temporary)
                if staged_manifest != manifest:
                    raise Denied("Source changed while staging; retry from a stable candidate copy")
                pi_lock = None
                pi_durable_lock = None
                if not args.core_only:
                    common = ["--engine-strict", "--no-audit", "--no-fund"]
                    # Permit npm's own script policy when opted in; on npm v12 removing
                    # --ignore-scripts still defers to allowScripts default-deny, so the flag
                    # is requested permission, not proof scripts ran. Default stays deny-all.
                    scripts = [] if args.allow_install_scripts else ["--ignore-scripts"]
                    for adapter_name in ("pi", "pi-durable"):
                        adapter = temporary / f"adapters/{adapter_name}"
                        if not (adapter / "package-lock.json").exists():
                            command(["npm", "install", "--package-lock-only", *common, "--ignore-scripts"], cwd=adapter)
                        command(["npm", "ci", *common, *scripts], cwd=adapter)
                        if adapter_name == "pi":
                            pi = adapter / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
                            result = subprocess.check_output([node, pi, "--version"], text=True, timeout=30).strip()
                        command([node, str(adapter / "launcher.mjs"), "--check-contract"], cwd=adapter)
                        if adapter_name == "pi":
                            pi_lock = digest((adapter / "package-lock.json").read_bytes())
                        else:
                            pi_durable_lock = digest((adapter / "package-lock.json").read_bytes())
                write_json(temporary / "source-manifest.json", manifest)
                write_json(temporary / "installation.json", {"version": __version__, "source_sha256": sha,
                            "installed_at": now(), "mode": "core-only" if args.core_only else "pi",
                            "pi_version": None if args.core_only else result, "pi_lock_sha256": pi_lock,
                            "pi_durable_lock_sha256": pi_durable_lock,
                            "node": node, "install_scripts_enabled": args.allow_install_scripts,
                            # Requested permission, not proof scripts ran (npm v12 may still deny via allowScripts).
                            })
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
    p.add_argument("--allow-install-scripts", action="store_true", help="Permit dependency lifecycle scripts under npm's own policy after reviewing the lock")
    p.add_argument("--allow-mode-change", action="store_true", help="Permit promotion across core-only/pi modes after review")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--stage-only", action="store_true", help="Stage a release without promoting it")
    group.add_argument("--activate", action="store_true", help="Promote after verification; existing services must be stopped")
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
    except StagingInterrupted as exc:
        print(json.dumps({"error": f"Installation interrupted by signal {exc.signum}"}), file=sys.stderr)
        return 128 + exc.signum
    except (MizuError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return getattr(exc, "code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
