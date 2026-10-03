"""An opt-in, paid end-to-end engine test. It cannot change a real project."""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
import uuid
from pathlib import Path

from . import platform as _platform
from .config import Config
from .errors import ConfigError, Denied
from .fs import mkdir, now, write_json
from .project import initialize
from .runtime import Engine


def live(config: Config, profile: str | None = None, role_name: str = "consult") -> dict:
    if _platform.is_root():
        raise Denied("Live tests must run as an unprivileged user")
    if role_name not in config.roles:
        raise ConfigError(f"Live probe role is not configured: {role_name}")
    role = config.roles[role_name]
    from .runtime import check_consult_role
    check_consult_role(role_name, role)
    role = dataclasses.replace(role, profile=profile or role.profile, workspace="read",
                               capabilities=("files", "read", "finish"), on_change=False)
    limits = dataclasses.replace(config.limits, requests_per_run=min(2, config.limits.requests_per_run),
                                 tools_per_run=min(5, config.limits.tools_per_run),
                                 run_seconds=min(120, config.limits.run_seconds))
    config = dataclasses.replace(config, roles={**config.roles, role_name: role}, limits=limits)
    challenge = "mizu-probe-" + uuid.uuid4().hex[:12]
    name = "smoke-" + uuid.uuid4().hex[:16]
    mkdir(config.data)
    project = None
    with tempfile.TemporaryDirectory(prefix="smoke-source-", dir=config.data) as temporary:
        root = Path(temporary)
        source = root / "source"
        mkdir(source)
        (source / "probe.txt").write_text(challenge)
        goal = root / "goal.md"
        # Test-only probe vector, not operator policy.
        goal.write_text("Read probe.txt. Then call mizu_finish alone with outcome 'wait' and summary exactly the file contents. Do nothing else.")
        evidence = config.data / "validation" / (name + ".project")
        try:
            project = initialize(config, name, source, goal, [role_name], [], armed=True)
            result = Engine(config, ephemeral=True).run(project, role_name)
            if result["finish"]["summary"].strip() != challenge:
                raise Denied("Engine ran, but the live smoke did not return the requested evidence")
            engine = config.engine(role.profile)
            checks = {
                "pi": ["trusted extension handshake", "exact model selection",
                       "provider-request admission", "custom tool round trip",
                       "finish followed by agent_settled"],
                "codex": ["trusted argv with configured sandbox", "isolated Codex home",
                          "required MCP bridge", "shared budget admission",
                          "finish round trip"],
                "claude": ["trusted argv with MCP-only allowlist", "per-run MCP config",
                           "required MCP bridge", "shared budget admission",
                           "finish round trip"],
            }[engine]
            report = {"status": "pass", "time": now(), "engine": engine, "checks": checks,
                      "result": result, "evidence": str(evidence)}
            write_json(config.data / "validation" / f"{name}.json", report)
            return report
        except BaseException as exc:
            write_json(config.data / "validation" / f"{name}.json", {"status": "fail", "time": now(), "error": str(exc), "evidence": str(evidence) if project else None})
            raise
        finally:
            if project:
                project.set_control(armed=False, paused=True)
                mkdir(evidence.parent)
                shutil.move(str(project.root), str(evidence))
