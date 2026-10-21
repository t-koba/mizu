"""Per-engine host-relative bounds for probes and managed startup handshakes.

Doctor and check-cli measure a one-shot probe from process spawn through exit.
Managed Pi, Claude and Codex runtimes measure spawn through their initial
protocol response. Those endpoints differ; the per-engine record keeps the
largest observed interval, and the shared bound is max(60 s, 12x sample),
ceiling at 150 s. A completed refusal is still a sample. A probe that cannot
complete within the measuring ceiling leaves the 60 s floor in place.

The managed runtime uses this bound only for initial startup/initialize.
Pi-durable keeps its existing transport-deadline path. The record is a local
host fact, not project policy.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from .errors import Busy, Denied, LimitExceeded
from .fs import lock, now, read_json, write_json

#: The floor, the multiplier and the ceiling of the host-relative bound.
ADAPTER_STARTUP_FLOOR_SECONDS = 60
ADAPTER_STARTUP_MULTIPLIER = 12
ADAPTER_STARTUP_CEILING_SECONDS = 150

#: Maximum duration of the one-shot probe used to collect a process-exit
#: measurement. If it does not finish, the probe uses the 60 s floor.
ADAPTER_STARTUP_MEASURE_CEILING_SECONDS = 150

#: The per-host record of the largest start-up cost observed per engine.
RECORD_NAME = 'adapter-startup.json'

#: The record is one number and one timestamp per engine; it stays small.
RECORD_MAX_BYTES = 8192

#: An engine name is a short identifier, never a path or a command.
ENGINE_NAME = re.compile(r'[a-z][a-z0-9-]{0,31}')


def adapter_startup_bound(measured_seconds) -> int:
    """This host's bound for one adapter child, from its measured start-up.

    Twelve times the measured start-up cost, at least 60 s and at most 150 s.
    An unmeasured cost keeps the floor: a child that has not answered cannot
    claim a wider bound than the floor allows.
    """
    if isinstance(measured_seconds, bool) or not isinstance(measured_seconds, (int, float)):
        return ADAPTER_STARTUP_FLOOR_SECONDS
    if measured_seconds <= 0:
        return ADAPTER_STARTUP_FLOOR_SECONDS
    return min(ADAPTER_STARTUP_CEILING_SECONDS,
               max(ADAPTER_STARTUP_FLOOR_SECONDS,
                   math.ceil(measured_seconds * ADAPTER_STARTUP_MULTIPLIER)))


def record_path(config) -> Path:
    """The per-host record of the measured adapter start-up costs."""
    return Path(config.data) / RECORD_NAME


def measured_startup(config, engine: str) -> float | None:
    """The largest start-up cost this host has measured for one engine.

    Schema: one entry per engine, each with the measured seconds and the
    time the measurement was taken. Bounds: the record is read under a
    fixed byte bound and holds one number per engine. Trust: local
    recorded state about this host, never a claim about a capability.
    Failure: a missing, unreadable or malformed record measures nothing,
    so the floor applies; it never widens the bound on its own.
    """
    if not isinstance(engine, str) or not ENGINE_NAME.fullmatch(engine):
        return None
    try:
        record = read_json(record_path(config), {}, max_bytes=RECORD_MAX_BYTES)
    except (OSError, ValueError, Busy, Denied, LimitExceeded):
        return None
    if not isinstance(record, dict):
        return None
    entry = record.get(engine)
    if not isinstance(entry, dict):
        return None
    seconds = entry.get('seconds')
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return float(seconds)


def record_startup(config, engine: str, seconds) -> None:
    """Keep the largest start-up cost this host has measured for one engine.

    A measurement never lowers the record: a host that was loaded once
    keeps the bound that load required. Recording never fails a run -- the
    record refines a bound, and the floor applies while it is absent.
    """
    if not isinstance(engine, str) or not ENGINE_NAME.fullmatch(engine):
        return
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return
    path = record_path(config)
    try:
        with lock(path.parent / (path.name + '.lock')):
            record = read_json(path, {}, max_bytes=RECORD_MAX_BYTES)
            if not isinstance(record, dict):
                record = {}
            entry = record.get(engine)
            previous = entry.get('seconds') if isinstance(entry, dict) else None
            if isinstance(previous, bool) or not isinstance(previous, (int, float)) or previous <= 0:
                previous = 0
            if seconds <= previous:
                return
            record[engine] = {'seconds': round(float(seconds), 3), 'measured_at': now()}
            write_json(path, {key: record[key] for key in sorted(record) if isinstance(record[key], dict)})
    except (OSError, ValueError, Busy, Denied, LimitExceeded):
        return


def startup_bound(config, engine: str) -> int:
    """The bound this host applies to one engine's child start-up."""
    return adapter_startup_bound(measured_startup(config, engine))


def observed_startup(config, engine: str, seconds) -> tuple:
    """Record a completed probe duration and return the conservative bound.

    Probe callers supply spawn-to-process-exit time. Runtime callers record
    spawn-to-first-protocol-response time, including refusals. These endpoints
    are intentionally not described as equivalent; the shared per-engine
    record retains the largest interval observed and derives the bound from it.
    If the record cannot be used, this call returns its local measurement and
    the bound that measurement requires.
    """
    if config is None or not isinstance(engine, str) or not ENGINE_NAME.fullmatch(engine):
        return float(seconds), adapter_startup_bound(seconds)
    record_startup(config, engine, seconds)
    cost = measured_startup(config, engine)
    if cost is None:
        cost = float(seconds)
    return cost, startup_bound(config, engine)
