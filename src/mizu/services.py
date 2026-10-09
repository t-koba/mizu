"""Render service definitions from one schedule model; no in-process scheduler.

The schedule lives in configuration (`daemon` / `interval_seconds` /
`calendar` per role). This module only spells it for the platform's own
service manager: systemd user units on Linux, launchd plists on macOS, Task
Scheduler XML on Windows. Each emitter is pure text over the same inputs;
`system=` exists so tests cover all three spellings on any host. Nothing is
started or enabled here on any platform.

Decision events and due structured waits wake scheduled roles before their
next interval: systemd path units on Linux and WatchPaths on macOS invoke
`mizu run --event`, which admits only on a subscribed decision event or a due
wait for that role and otherwise exits before any model use; the periodic
timers invoke plain `mizu run` on their own schedules unchanged. Task
Scheduler XML has no file trigger in this minimal schema, so Windows still
dispatches events when its timer fires.

Event triggers use systemd defaults with no trigger limit: admission is
state-based (`mizu run --event` exits before any model use when nothing is
due), so extra dispatches are cheap and a burst must not fail the trigger.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as _escape

from . import platform as _platform
from .config import Config
from .errors import Denied
from .fs import atomic_write, identifier, mkdir, sync_dir, digest, read_json, write_json, lock
from .project import Project

#: Definition file extensions each platform owns.
SERVICE_EXTENSIONS = {"linux": (".service", ".timer", ".path"), "macos": (".plist",), "windows": (".xml",)}

#: Fixed per-platform template floors (mechanism, not operator policy):
#: restart delay avoids a tight crash loop, timer accuracy/jitter avoids
#: thundering-herd wakeups, stop timeout bounds shutdown. Schedules
#: (daemon/interval/calendar) stay operator policy; these floors keep the
#: manager from busy-looping regardless of schedule choice.
RESTART_SEC = 15
ACCURACY_SEC = "10s"
RANDOMIZED_DELAY_SEC = "15s"
STOP_SEC = 45
#: The event service disables its own start limiter so an unsupported/edited
#: trigger can only cause sequential fast exits (admission exits before any
#: model use when nothing is due), never a start-limit-hit that stops
#: dispatching until reset by hand. The path trigger itself keeps systemd
#: defaults: a TriggerLimitBurst=1 floor fails the path unit on two writes
#: within the window and stops dispatching (observed 2026-10-09), so no
#: trigger limit is rendered here.
WINDOWS_RESTART_INTERVAL = "PT1M"
WINDOWS_RESTART_COUNT = 999


def quote(value: str) -> str:
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise Denied("Newlines are not valid in service arguments")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _xml(value: str) -> str:
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise Denied("Newlines are not valid in service arguments")
    return _escape(value)


def _scheduled_roles(config: Config, project: Project, executable: Path):
    """Yield (name, role, base, args) for roles with a schedule.

    Schema/bounds: at most one of daemon/interval/calendar per role (enforced
    at load). Trust: config only. Retry: none. Evidence: unit names.
    Failure: none (unscheduled roles skipped).
    """
    for name in project.roles:
        role = config.roles[name]
        if not (role.daemon or role.interval_seconds or role.calendar):
            continue
        base = f"mizu-{len(project.name)}-{identifier(project.name)}-{identifier(name)}"
        args = [str(executable.resolve()), "--config", str(config.file),
                "daemon" if role.daemon else "run", project.name, "--role", name]
        yield name, role, base, args


def event_watches(config: Config, project: Project) -> dict[str, list[str]]:
    """Event-trigger watch directories per scheduled role.

    Mechanism: roles with decision_events wake on decision-history and waits
    writes; write roles with a schedule additionally wake when their own
    structured waits come due. Daemon roles need no file trigger (their loop
    re-checks admission every idle tick via should_run) and unscheduled roles
    get nothing. Periodic discovery stays independent policy: timers and
    calendar entries keep their own schedules unchanged.
    """
    watches = {}
    for name in project.roles:
        role = config.roles[name]
        if role.daemon or not (role.interval_seconds or role.calendar):
            continue
        if not role.decision_events and role.workspace != "write":
            continue
        watches[name] = [str(project.root / "decision-history"), str(project.root / "waits")]
    return watches


def _render_systemd(config: Config, project: Project, executable: Path) -> dict[str, str]:
    result = {}
    watches = event_watches(config, project)
    for name, role, base, args in _scheduled_roles(config, project, executable):
        cleanup = [str(executable.resolve()), "--config", str(config.file),
                   "cleanup", project.name, "--role", name]
        service = ["[Unit]", f"Description=Mizu {project.name} / {name}", "After=network-online.target",
                   "", "[Service]", "Type=exec", "UMask=0077", "KillMode=control-group", "Delegate=yes",
                   f"TimeoutStopSec={STOP_SEC}", "ExecStart=" + " ".join(map(quote, args)),
                   "ExecStopPost=-" + " ".join(map(quote, cleanup)),
                   # These are expected operator/budget/lock outcomes, not service failures.
                   "SuccessExitStatus=69 75 76 77 78 130"]
        if role.daemon:
            service.extend(["Restart=on-failure", f"RestartSec={RESTART_SEC}", "", "[Install]", "WantedBy=default.target"])
        result[base + ".service"] = "\n".join(service) + "\n"
        if role.daemon:
            continue
        timer = ["[Unit]", f"Description=Mizu schedule for {project.name} / {name}", "", "[Timer]",
                 f"Unit={base}.service", "Persistent=true", f"AccuracySec={ACCURACY_SEC}", f"RandomizedDelaySec={RANDOMIZED_DELAY_SEC}"]
        if role.interval_seconds:
            timer.extend(["OnBootSec=1min", f"OnUnitInactiveSec={role.interval_seconds}s"])
        else:
            timer.extend(f"OnCalendar=*-*-* {t}:00" + ("" if config.timezone == "local" else " " + config.timezone) for t in role.calendar)
        timer.extend(["", "[Install]", "WantedBy=timers.target"])
        result[base + ".timer"] = "\n".join(timer) + "\n"
        if name in watches:
            event_base = base + "-event"
            event_args = [str(executable.resolve()), "--config", str(config.file),
                          "run", project.name, "--role", name, "--event"]
            event_service = ["[Unit]", f"Description=Mizu event dispatch for {project.name} / {name}", "After=network-online.target",
                       "StartLimitIntervalSec=0",
                       "", "[Service]", "Type=oneshot", "UMask=0077", "KillMode=control-group", "Delegate=yes",
                       f"TimeoutStopSec={STOP_SEC}", "ExecStart=" + " ".join(map(quote, event_args)),
                       "ExecStopPost=-" + " ".join(map(quote, cleanup)),
                       "SuccessExitStatus=69 75 76 77 78 130"]
            result[event_base + ".service"] = "\n".join(event_service) + "\n"
            trigger = ["[Unit]", f"Description=Mizu event trigger for {project.name} / {name}", "",
                       "[Path]",
                       *(f"PathChanged={watch}" for watch in watches[name]),
                       f"Unit={event_base}.service", "", "[Install]", "WantedBy=default.target"]
            result[event_base + ".path"] = "\n".join(trigger) + "\n"
    return result


def _plist_args(args: list[str]) -> str:
    return "".join(f"    <string>{_xml(a)}</string>\n" for a in args)


def _render_launchd(config: Config, project: Project, executable: Path) -> dict[str, str]:
    result = {}
    entry = executable.resolve()
    resolved = str(entry)
    watches = event_watches(config, project)
    for name, role, base, _ in _scheduled_roles(config, project, executable):
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
        if not role.daemon and name in watches:
            event_base = base + "-event"
            event_args = [resolved, "--config", str(config.file),
                          "run", project.name, "--role", name, "--event"]
            event_body = [f"  <key>Label</key><string>{event_base}</string>",
                          "  <key>ProgramArguments</key>", "  <array>",
                          _plist_args(event_args).rstrip("\n"), "  </array>",
                          "  <key>WatchPaths</key>", "  <array>",
                          "".join(f"    <string>{_xml(watch)}</string>\n" for watch in watches[name]).rstrip("\n"),
                          "  </array>"]
            result[event_base + ".plist"] = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                                   '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                                   '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                                   '<plist version="1.0">\n<dict>\n' + "\n".join(event_body) +
                                   '\n</dict>\n</plist>\n')
    return result


def _windows_arguments(args: list[str]) -> str:
    for part in args:
        if any(c in part for c in ("\n", "\r", "\x00")):
            raise Denied("Newlines are not valid in service arguments")
    return _escape(subprocess.list2cmdline(args))


def _render_windows(config: Config, project: Project, executable: Path) -> dict[str, str]:
    # No file trigger in this minimal Task Scheduler schema: event admission
    # still applies when the timer fires (Engine.run checks decisions and
    # due waits on every dispatch); file-triggered wake before the interval
    # is provided where the platform supports it (systemd path, launchd
    # WatchPaths).
    result = {}
    entry = executable.resolve()
    if entry.suffix.lower() in (".cmd", ".bat"):
        entry = entry.with_suffix("")
        if not entry.is_file():
            raise Denied("Windows service needs the Mizu Python entry point beside its command wrapper")
    resolved = str(entry)
    zone_suffix = "+00:00" if config.timezone == "UTC" else ""
    for name, role, base, _ in _scheduled_roles(config, project, executable):
        args = [resolved, "--config", str(config.file),
                "daemon" if role.daemon else "run", project.name, "--role", name]
        # Task Scheduler starts a real executable; mizu is a Python script.
        command, arguments = _xml(sys.executable), _windows_arguments(args)
        if role.daemon:
            trigger = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
            settings = ("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
                        f"<RestartOnFailure><Interval>{WINDOWS_RESTART_INTERVAL}</Interval><Count>{WINDOWS_RESTART_COUNT}</Count></RestartOnFailure>")
        elif role.interval_seconds:
            trigger = (f"<TimeTrigger><StartBoundary>2026-01-01T00:00:00{zone_suffix}</StartBoundary>"
                       f"<Repetition><Interval>PT{role.interval_seconds}S</Interval>"
                       "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
                       "<Enabled>true</Enabled></TimeTrigger>")
            settings = "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        else:
            trigger = "".join(
                f"<CalendarTrigger><StartBoundary>2026-01-01T{t}:00{zone_suffix}</StartBoundary>"
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
            f"  <Settings>{settings}<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings>\n"
            f'  <Actions Context="Author"><Exec><Command>{command}</Command>'
            f"<Arguments>{arguments}</Arguments></Exec></Actions>\n"
            "</Task>\n")
    return result


def render(config: Config, project: Project, executable: Path, *, system: str | None = None) -> dict[str, str]:
    """Render service definitions for one platform. `system=` selects the
    spelling explicitly so tests cover all three on any host."""
    name = system or _platform.SYSTEM
    if name in ("macos", "windows") and any(config.roles[r].calendar for r in project.roles):
        supported = ("local", "UTC") if name == "windows" else ("local",)
        if config.timezone not in supported:
            raise Denied(f"{name} calendar supports {supported}; IANA schedules require Linux")
    if name == "windows":
        for r in project.roles:
            interval = config.roles[r].interval_seconds
            if interval and not 60 <= interval <= 31 * 86400:
                raise Denied("Windows task repetition must be between 60 seconds and 31 days")
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
    owner = directory / (".mizu-" + digest(project.name.encode())[:24] + ".json")
    verification = "not_run: native validator unavailable"
    with lock(directory / ".mizu-services.lock"):
        previous = read_json(owner, {})
        if previous and (previous.get("project") != project.name or previous.get("system") != name):
            raise Denied("Service ownership record conflicts")
        owned = previous.get("files", {})
        if not isinstance(owned, dict) or any(Path(unit).name != unit or "\\" in unit or unit in (".", "..") for unit in owned):
            raise Denied("Invalid service ownership inventory")
        for unit, sha in owned.items():
            if Path(unit).name != unit or Path(unit).suffix not in SERVICE_EXTENSIONS[name]:
                raise Denied("Invalid owned service path")
            path = directory / unit
            if path.is_symlink() or (path.exists() and digest(path.read_bytes()) != sha):
                raise Denied("Owned service definition changed; review before replacing: " + unit)
        for unit in units:
            path = directory / unit
            if (path.exists() or path.is_symlink()) and unit not in owned:
                raise Denied("Refusing to overwrite an unowned definition: " + unit)
        with tempfile.TemporaryDirectory(prefix=".mizu-services-", dir=directory) as tmp:
            stage = Path(tmp)
            for unit, content in units.items():
                atomic_write(stage / unit, content.encode())
            if name == "linux":
                analyzer = shutil.which("systemd-analyze")
                if analyzer:
                    try:
                        result = subprocess.run([analyzer, "verify", *(str(stage / u) for u in sorted(units))],
                                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
                    except (OSError, subprocess.SubprocessError) as exc:
                        raise Denied(f"Unit verification could not run: {exc}") from exc
                    if result.returncode:
                        raise Denied("Generated units failed verification: " + result.stderr[-2000:])
                    verification = "pass"
            else:
                for content in units.values():
                    plistlib.loads(content.encode()) if name == "macos" else ET.fromstring(content)
                verification = "syntax-pass; native registration not_run"
            for unit, content in units.items():
                atomic_write(directory / unit, content.encode())
            for stale in owned.keys() - units.keys():
                (directory / stale).unlink(missing_ok=True)
            write_json(owner, {"project": project.name, "system": name,
                               "files": {u: digest(c.encode()) for u, c in units.items()},
                               "timezone": config.timezone if name == "linux" or (name == "windows" and config.timezone == "UTC") else "OS local"})
            sync_dir(directory)
    if name == "linux":
        triggered = ({unit[:-len(".timer")] for unit in units if unit.endswith(".timer")} |
                     {unit[:-len(".path")] for unit in units if unit.endswith(".path")})
        start = [unit for unit in units if unit.endswith((".timer", ".path")) or
                 (unit.endswith(".service") and unit[:-8] not in triggered)]
    else:
        start = sorted(units)
    return {"system": name, "directory": str(directory), "written": sorted(units), "enable_units": sorted(start),
            "verification": verification,
            "timezone": config.timezone if name == "linux" or (name == "windows" and config.timezone == "UTC") else "OS local",
            "note": _ENABLE_NOTES[name]}
