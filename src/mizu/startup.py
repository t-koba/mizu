"""This host's own adapter start-up cost, and the bound that child needs.

Every child the product starts is dominated by the same cost: loading its
SDK. A fixed bound kills that child on a loaded host before it can report,
and the kill was read as a missing capability or a failed handshake. This
host has been observed to need 3.2-4.3 s idle and 8.9-44.5 s under a 32-way
load, so the bound is this host's own measurement rather than a constant:
twelve times the measured cost, at least a floor and at most a ceiling.

The floor sits above the worst cost this host has been observed to need and
the ceiling stays far below the check phase's own budget. The measurement is
a host fact, not a project fact: it is recorded per engine under the shared
data directory, keeps the largest cost observed, and is read by every path
that starts that engine's child, so the product never holds two answers for
one child's start-up.
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

#: The measuring spawn carries its own ceiling: a host that cannot answer
#: the measurement leaves the floor in place instead of hanging the phase.
#: The launcher harness measures the identical child under this same
#: ceiling, so the two surfaces never derive two different bounds for one
#: child's start-up.
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
    """The cost and the bound one observed start-up answers with.

    One rule for every surface that starts an engine's child: an observed
    cost joins this host's record for that engine, the bound is the one
    that record derives, and the cost returned beside the bound is the
    cost the bound was derived from, so the published pair satisfies the
    published rule.

    An answered child is a measurement even when its answer was a failure:
    the launcher harness measures the identical child by its first answer,
    which is a refusal, so a surface that treats a failure as an
    unmeasured child holds two answers for one child's start-up. A cost
    that cannot be recorded -- no configuration, or no engine name --
    keeps the local measurement and the bound that measurement requires.
    """
    if config is None or not isinstance(engine, str) or not ENGINE_NAME.fullmatch(engine):
        return float(seconds), adapter_startup_bound(seconds)
    record_startup(config, engine, seconds)
    cost = measured_startup(config, engine)
    if cost is None:
        cost = float(seconds)
    return cost, startup_bound(config, engine)
