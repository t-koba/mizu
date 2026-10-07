"""An opt-in, paid end-to-end engine test. It cannot change a real project.

Schema: ``live(config, profile, role_name)`` runs the named consult role's
engine against a temporary project containing only ``probe.txt``.
Bounds: paid spend is capped at fixed ``SMOKE_REQUESTS/TOOLS/SECONDS``
(clamped below operator limits, never raised). Trust: the probe system
prompt is the fixed ``SMOKE_PROBE_POLICY`` written per run under
``<data>/validation/smoke-probe-policy.md``; operator consult policy text
is never offered, so smoke tests the engine/provider path only.
Retry/cancellation: single run under the run deadline; no retry.
Evidence: pass/fail JSON plus the moved project under ``<data>/validation``.
Failure: ``Denied`` when the engine does not return the probe contents.
"""
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

#: Fixed probe budgets (mechanism constants, not knobs): the paid live probe
#: caps spend while tolerating one benign extra step. On 2026-10-03 the
#: contributor model listed files before reading probe.txt (3 sequences)
#: and exhausted the old cap of 2. Four admits files plus read plus finish
#: plus one auxiliary Pi request; tools stay 5 and wall-clock stays 120 s.
SMOKE_REQUESTS_PER_RUN = 4
SMOKE_TOOLS_PER_RUN = 5
SMOKE_RUN_SECONDS = 120

#: Fixed probe system prompt (mechanism, not operator policy): the live probe
#: must read ``probe.txt`` even when the operator's consult policy says to
#: advise from the snapshot without reading files. Mirrors the probe goal
#: so engine and goal agree on the operative instruction; operator consult
#: text is never used here.
SMOKE_PROBE_POLICY = (
    "You are the Mizu live smoke probe. Read the file probe.txt in the"
    " workspace root (exact path probe.txt) with a single read. Do not list"
    " files first; the workspace contains only probe.txt. Then call"
    " mizu_finish alone with outcome 'wait' and summary exactly the file"
    " contents. Do nothing else.\n"
)


def live(config: Config, profile: str | None = None, role_name: str = "consult") -> dict:
    if _platform.is_root():
        raise Denied("Live tests must run as an unprivileged user")
    if role_name not in config.roles:
        raise ConfigError(f"Live probe role is not configured: {role_name}")
    role = config.roles[role_name]
    from .runtime import check_consult_role
    check_consult_role(role_name, role)
    if role.selector and not profile:
        raise ConfigError("A selector role requires an explicit --profile for smoke")
    mkdir(config.data / "validation")
    probe_policy = config.data / "validation" / "smoke-probe-policy.md"
    probe_policy.write_bytes(SMOKE_PROBE_POLICY.encode("utf-8"))
    role = dataclasses.replace(role, profile=profile or role.profile, selector="", workspace="read",
                               capabilities=("files", "read", "finish"), on_change=False,
                               policy=(probe_policy,))
    # Probe budgets stay fixed mechanism constants per ADR-006/ADR-013, not
    # knobs: 4 requests tolerate one benign extra listing (files + read +
    # finish is 3 sequences) plus one auxiliary Pi inference without opening
    # paid spend; tools stay 5 and wall-clock stays 120 s.
    limits = dataclasses.replace(config.limits, requests_per_run=min(SMOKE_REQUESTS_PER_RUN, config.limits.requests_per_run),
                                 tools_per_run=min(SMOKE_TOOLS_PER_RUN, config.limits.tools_per_run),
                                 run_seconds=min(SMOKE_RUN_SECONDS, config.limits.run_seconds))
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
        goal.write_text("Read the file probe.txt in the workspace root (exact path probe.txt) with a single read. Do not list files first; the workspace contains only probe.txt. Then call mizu_finish alone with outcome 'wait' and summary exactly the file contents. Do nothing else.")
        evidence = config.data / "validation" / (name + ".project")
        try:
            project = initialize(config, name, source, goal, [role_name], [], armed=True)
            result = Engine(config, ephemeral=True).run(project, role_name)
            if result["finish"]["summary"].strip() != challenge:
                raise Denied("Engine ran, but the live smoke did not return the requested evidence")
            engine = config.engine(role.profile)
            checks = {
                "pi-durable": ["trusted extension handshake", "exact model selection",
                       "provider-request admission", "custom tool round trip",
                       "finish followed by agent_settled", "durable turn persistence"],
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
