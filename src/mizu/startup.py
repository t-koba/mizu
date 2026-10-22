"""Per-engine host-relative bounds for probes and managed startup handshakes.

Doctor and check-cli measure a one-shot probe from process spawn through exit.
Managed Pi, Claude and Codex runtimes measure spawn through their initial
protocol response. The two endpoints are distinct measurements, so the record
keeps the largest interval observed for an engine together with the endpoint
that measured it, and the bound derives from that interval: max(60 s, 12x
sample), ceiling at 150 s. A completed refusal is still a sample. A probe
that cannot complete within the measuring ceiling leaves the 60 s floor in
place, and a measurement that cannot be shared leaves the floor in place too.

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

#: The measurement endpoints an interval can come from. A one-shot probe
#: measures spawn through process exit; a managed runtime measures spawn
#: through its first protocol response.
PROBE_ENDPOINT = 'probe'
RUNTIME_ENDPOINT = 'runtime'
ENDPOINTS = (PROBE_ENDPOINT, RUNTIME_ENDPOINT)

#: The per-host record of the largest start-up cost observed per engine.
RECORD_NAME = 'adapter-startup.json'

#: The record's write lock, named beside it.
RECORD_LOCK_NAME = RECORD_NAME + '.lock'

#: The product's own host-local state files. They hold one host's
#: measurements, not the product's source, so a capture leaves them out
#: and a commit never carries them.
HOST_STATE_FILES = frozenset({RECORD_NAME, RECORD_LOCK_NAME})

#: The record is one number, one endpoint and one timestamp per engine; it
#: stays small.
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


def startup_sample(config, engine: str) -> dict | None:
    """The retained start-up sample for one engine, with its endpoint.

    Schema: one entry per engine, each with the measured seconds, the
    endpoint that measured them ('probe' or 'runtime') and the time the
    measurement was taken. Bounds: the record is read under a fixed byte
    bound and holds one sample per engine. Trust: local recorded state
    about this host, never a claim about a capability. Failure: a missing,
    unreadable or malformed record measures nothing, so the floor applies;
    it never widens the bound on its own.
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
    endpoint = entry.get('endpoint')
    return {'seconds': float(seconds),
            'endpoint': endpoint if endpoint in ENDPOINTS else None,
            'measured_at': entry.get('measured_at')}


def measured_startup(config, engine: str) -> float | None:
    """The largest start-up cost this host has measured for one engine."""
    sample = startup_sample(config, engine)
    return sample['seconds'] if sample else None


def record_startup(config, engine: str, seconds, endpoint: str) -> None:
    """Keep the largest start-up cost this host has measured for one engine.

    A measurement never lowers the record: a host that was loaded once
    keeps the bound that load required. The endpoint that measured the
    retained interval is kept with it, so the published evidence names the
    measurement the bound came from. A record file that cannot be parsed
    holds no measurement, so the next one replaces it instead of leaving
    the bound at the floor for ever. Recording never fails a run -- the
    record refines a bound, and the floor applies while it is absent.
    """
    if not isinstance(engine, str) or not ENGINE_NAME.fullmatch(engine):
        return
    if endpoint not in ENDPOINTS:
        return
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return
    path = record_path(config)
    try:
        with lock(path.parent / (path.name + '.lock')):
            try:
                record = read_json(path, {}, max_bytes=RECORD_MAX_BYTES)
            except ValueError:
                # A record that cannot be parsed holds no measurement: the
                # next one replaces it, so the host recovers instead of
                # publishing the floor for ever.
                record = {}
            if not isinstance(record, dict):
                record = {}
            entry = record.get(engine)
            previous = entry.get('seconds') if isinstance(entry, dict) else None
            if isinstance(previous, bool) or not isinstance(previous, (int, float)) or previous <= 0:
                previous = 0
            if seconds <= previous:
                return
            record[engine] = {'seconds': round(float(seconds), 3), 'endpoint': endpoint,
                              'measured_at': now()}
            write_json(path, {key: record[key] for key in sorted(record) if isinstance(record[key], dict)})
    except (OSError, ValueError, Busy, Denied, LimitExceeded):
        return


def startup_bound(config, engine: str) -> int:
    """The bound this host applies to one engine's child start-up."""
    return adapter_startup_bound(measured_startup(config, engine))


def observed_startup(config, engine: str, seconds, endpoint: str) -> tuple:
    """Record a completed measurement and return the cost, bound and endpoint.

    Probe callers supply spawn-to-process-exit time; runtime callers supply
    spawn-to-first-protocol-response time, including refusals. The two
    endpoints are distinct measurements, so the record keeps the largest
    interval observed for the engine together with the endpoint that
    measured it, and the published pair names that endpoint.

    The published cost is the retained interval and the published bound is
    the one that interval derives, so the published pair satisfies the
    published rule. A measurement that cannot be recorded or read back is
    not published as a cost at all: the product publishes no cost and the
    floor bound, which is the answer every surface gives while the host's
    shared record holds nothing.
    """
    if config is None or not isinstance(engine, str) or not ENGINE_NAME.fullmatch(engine):
        return None, ADAPTER_STARTUP_FLOOR_SECONDS, None
    record_startup(config, engine, seconds, endpoint)
    sample = startup_sample(config, engine)
    if sample is None:
        return None, ADAPTER_STARTUP_FLOOR_SECONDS, None
    return sample['seconds'], adapter_startup_bound(sample['seconds']), sample['endpoint']
