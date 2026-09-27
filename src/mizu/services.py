"""Render service definitions from one schedule model; no in-process scheduler.

The schedule lives in configuration (`daemon` / `interval_seconds` /
`calendar` per role). This module only spells it for the platform's own
service manager: systemd user units on Linux, launchd plists on macOS, Task
Scheduler XML on Windows. Each emitter is pure text over the same inputs;
`system=` exists so tests cover all three spellings on any host. Nothing is
started or enabled here on any platform.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape as _escape

from . import platform as _platform
from .config import Config
from .errors import Denied
from .fs import atomic_write, identifier, mkdir, sync_dir
from .project import Project

#: Definition file extensions each platform owns.
SERVICE_EXTENSIONS = {"linux": (".service", ".timer"), "macos": (".plist",), "windows": (".xml",)}


def quote(value: str) -> str:
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise Denied("Newlines are not valid in service arguments")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _xml(value: str) -> str:
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise Denied("Newlines are not valid in service arguments")
    return _escape(value)


def _render_systemd(config: Config, project: Project, executable: Path) -> dict[str, str]:
    result = {}
    for name in project.roles:
        role = config.roles[name]
        if not (role.daemon or role.interval_seconds or role.calendar):
            continue
        base = f"mizu-{identifier(project.name)}-{identifier(name)}"
        args = [str(executable.resolve()), "--config", str(config.file),
                "daemon" if role.daemon else "run", project.name, "--role", name]
        cleanup = [str(executable.resolve()), "--config", str(config.file),
                   "cleanup", project.name, "--role", name]
        service = ["[Unit]", f"Description=Mizu {project.name} / {name}", "After=network-online.target",
                   "", "[Service]", "Type=exec", "UMask=0077", "KillMode=control-group", "Delegate=yes",
                   "TimeoutStopSec=45", "ExecStart=" + " ".join(map(quote, args)),
                   "ExecStopPost=-" + " ".join(map(quote, cleanup)),
                   # These are expected operator/budget/lock outcomes, not service failures.
                   "SuccessExitStatus=69 75 76 77 78 130"]
        if role.daemon:
            service.extend(["Restart=on-failure", "RestartSec=15", "", "[Install]", "WantedBy=default.target"])
        result[base + ".service"] = "\n".join(service) + "\n"
        if role.daemon:
            continue
        timer = ["[Unit]", f"Description=Mizu schedule for {project.name} / {name}", "", "[Timer]",
                 f"Unit={base}.service", "Persistent=true", "AccuracySec=10s", "RandomizedDelaySec=15s"]
        if role.interval_seconds:
            timer.extend(["OnBootSec=1min", f"OnUnitInactiveSec={role.interval_seconds}s"])
        else:
            timer.extend(f"OnCalendar=*-*-* {t}:00 {config.timezone}" for t in role.calendar)
        timer.extend(["", "[Install]", "WantedBy=timers.target"])
        result[base + ".timer"] = "\n".join(timer) + "\n"
    return result


def _plist_args(args: list[str]) -> str:
    return "".join(f"    <string>{_xml(a)}</string>\n" for a in args)


def _render_launchd(config: Config, project: Project, executable: Path) -> dict[str, str]:
    result = {}
    resolved = str(executable.resolve())
    for name in project.roles:
        role = config.roles[name]
        if not (role.daemon or role.interval_seconds or role.calendar):
            continue
        base = f"mizu-{identifier(project.name)}-{identifier(name)}"
        args = [resolved, "--config", str(config.file),
                "daemon" if role.daemon else "run", project.name, "--role", name]
        body = [f"  <key>Label</key><string>{base}</string>",
                "  <key>ProgramArguments</key>", "  <array>",
                _plist_args(args).rstrip("\n"), "  </array>"]
        if role.daemon:
            body.extend(["  <key>KeepAlive</key><true/>", "  <key>RunAtLoad</key><true/>"])
        elif role.interval_seconds:
            body.append(f"  <key>StartInterval</key><integer>{role.interval_seconds}</integer>")
        else:
            entries = "".join(
                f"    <dict><key>Hour</key><integer>{int(t[:2])}</integer>"
                f"<key>Minute</key><integer>{int(t[3:])}</integer></dict>\n" for t in role.calendar)
            body.extend(["  <key>StartCalendarInterval</key>", "  <array>",
                         entries.rstrip("\n"), "  </array>"])
        result[base + ".plist"] = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                                   '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                                   '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                                   '<plist version="1.0">\n<dict>\n' + "\n".join(body) +
                                   '\n</dict>\n</plist>\n')
    return result


def _windows_arguments(args: list[str]) -> str:
    parts = []
    for part in args:
        if any(c in part for c in ("\n", "\r", "\x00")):
            raise Denied("Newlines are not valid in service arguments")
        parts.append(f'"{part}"' if " " in part else part)
    return _escape(" ".join(parts))


def _render_windows(config: Config, project: Project, executable: Path) -> dict[str, str]:
    result = {}
    resolved = str(executable.resolve())
    for name in project.roles:
        role = config.roles[name]
        if not (role.daemon or role.interval_seconds or role.calendar):
            continue
        base = f"mizu-{identifier(project.name)}-{identifier(name)}"
        args = [resolved, "--config", str(config.file),
                "daemon" if role.daemon else "run", project.name, "--role", name]
        command, arguments = _xml(args[0]), _windows_arguments(args[1:])
        if role.daemon:
            trigger = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
            settings = ("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
                        "<RestartOnFailure><Interval>PT15S</Interval><Count>999</Count></RestartOnFailure>")
        elif role.interval_seconds:
            trigger = (f"<TimeTrigger><StartBoundary>2026-01-01T00:00:00</StartBoundary>"
                       f"<Repetition><Interval>PT{role.interval_seconds}S</Interval>"
                       "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
                       "<Enabled>true</Enabled></TimeTrigger>")
            settings = "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        else:
            trigger = "".join(
                f"<CalendarTrigger><StartBoundary>2026-01-01T{t}:00</StartBoundary>"
                "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
                "<Enabled>true</Enabled></CalendarTrigger>" for t in role.calendar)
            settings = "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        result[base + ".xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            f"  <RegistrationInfo><Description>Mizu {project.name} / {name}</Description></RegistrationInfo>\n"
            f"  <Triggers>{trigger}</Triggers>\n"
            '  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>'
            "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
            f"  <Settings>{settings}<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings>\n"
            f'  <Actions Context="Author"><Exec><Command>{command}</Command>'
            f"<Arguments>{arguments}</Arguments></Exec></Actions>\n"
            "</Task>\n")
    return result


def render(config: Config, project: Project, executable: Path, *, system: str | None = None) -> dict[str, str]:
    """Render service definitions for one platform. `system=` selects the
    spelling explicitly so tests cover all three on any host."""
    name = system or _platform.SYSTEM
    if name == "macos":
        return _render_launchd(config, project, executable)
    if name == "windows":
        return _render_windows(config, project, executable)
    if name != "linux":
        raise Denied(f"Unsupported service platform: {name}")
    return _render_systemd(config, project, executable)


_ENABLE_NOTES = {
    "linux": "Nothing was started or enabled. Review units, then daemon-reload and enable explicitly.",
    "macos": ("Nothing was started or enabled. Review plists, then "
              "`launchctl bootstrap gui/$UID <file>.plist` explicitly."),
    "windows": ("Nothing was started or enabled. Review task files, then "
                "`schtasks /Create /TN <name> /XML <file>.xml` explicitly."),
}


def install(config: Config, project: Project, executable: Path, destination: Path | None = None,
            *, system: str | None = None) -> dict:
    name = system or _platform.SYSTEM
    if name not in SERVICE_EXTENSIONS:
        raise Denied(f"Unsupported service platform: {name}")
    directory = destination or _platform.service_dir(name)
    mkdir(directory)
    units = render(config, project, executable, system=name)
    if not units:
        raise Denied("No role has daemon/interval/calendar; nothing to install")
    for unit, content in units.items():
        atomic_write(directory / unit, content.encode())
    # Remove stale definitions for roles that no longer have a schedule.
    extensions = SERVICE_EXTENSIONS[name]
    for stale in directory.glob(f"mizu-{project.name}-*"):
        if stale.suffix not in extensions:
            continue
        if stale.name not in units:
            stale.unlink()
    sync_dir(directory)
    verification = "skipped: systemd-analyze not available"
    if name == "linux":
        analyzer = shutil.which("systemd-analyze")
        if analyzer is not None:
            try:
                result = subprocess.run(
                    [analyzer, "verify", *(str(directory / unit) for unit in sorted(units))],
                    capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as exc:
                raise Denied(f"Unit verification could not run: {exc}") from exc
            if result.returncode != 0:
                raise Denied("Generated units failed verification: " + result.stderr[-2000:])
            verification = "pass"
    if name == "linux":
        start = [unit for unit in units if unit.endswith(".timer") or
                 (unit.endswith(".service") and unit[:-8] + ".timer" not in units)]
    else:
        start = sorted(units)
    return {"system": name, "directory": str(directory), "written": sorted(units), "enable_units": sorted(start),
            "verification": verification if name == "linux" else "not_run: Linux only",
            "note": _ENABLE_NOTES[name]}
