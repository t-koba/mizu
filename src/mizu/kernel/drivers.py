"""Generic observation drivers: bounded, operator-owned host programs.

A driver is an operator-configured command that speaks one JSON
request/response protocol over stdin/stdout. The kernel passes a request and
returns a response without interpreting its contents; an observing driver
reports what it found as plain objects that are appended to a declared queue.
No name of an external system, record, state, event or workflow appears here:
commands, bounds and destination queues are deployment configuration.

Schema: ``[drivers.<name>]`` declares ``command``, ``timeout_seconds``,
``max_bytes``, ``observation_queue`` and ``diagnostic_bytes``. A response is
one JSON object; an observation response carries an ``observations`` list of
plain objects. Bounds: the canonical request and the program's combined output
are each capped at ``max_bytes``; a single call is bounded by
``timeout_seconds`` and an observation response holds at most 1024 objects.
Trust: the command is operator configuration, never model-provided argv, and
never run in the sandbox. Requests and responses, including the bounded stderr
tail attached to a nonzero-exit failure, stay external-untrusted. Retry: one
invocation per call under an optional ``cancel`` callback, with no automatic
replay. Evidence: callers bind the returned dict and append receipt to their
own records; the kernel records no driver-specific evidence. Failure:
fail-closed ``Denied`` on any bound, exit, transport or decoding violation, and
on any observation body the destination queue refuses.
"""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ..errors import ConfigError, Denied
from ..fs import canonical, loads_finite
from ..process import (
    ADAPTER_DIAGNOSTIC_BYTES_MAX, adapter_failure_diagnostic, run,
)
from .queues import NAME, allows

#: One call wall deadline: one second to one day.
TIMEOUT_DEFAULT = 20.0
TIMEOUT_MIN = 1
TIMEOUT_MAX = 86400
#: Canonical request and combined program output byte cap: 1 KiB to 16 MiB.
MAX_BYTES_DEFAULT = 524288
MAX_BYTES_MIN = 1024
MAX_BYTES_MAX = 16777216
#: Stderr tail attached to a nonzero-exit failure; zero disables it.
DIAGNOSTIC_BYTES_DEFAULT = 0
#: Upper observation count in one response; a fixed mechanism bound.
MAX_OBSERVATIONS = 1024
_DRIVER_KEYS = frozenset({"command", "timeout_seconds", "max_bytes",
                          "observation_queue", "diagnostic_bytes"})


@dataclasses.dataclass(frozen=True)
class Driver:
    name: str
    command: tuple[str, ...]
    timeout_seconds: float
    max_bytes: int
    observation_queue: str
    diagnostic_bytes: int


def _number(value: Any, where: str, low: int, high: int, *, integral: bool = False):
    """Return a bounded finite number, coerced to int when bytes require it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a finite number in [{low}, {high}]")
    if isinstance(value, int):
        # No finite conversion is needed; huge integers are rejected by the
        # range check rather than overflowing a float conversion.
        finite = True
    else:
        finite = math.isfinite(value)
    if (not finite or not low <= value <= high
            or (integral and int(value) != value)):
        raise ConfigError(f"{where} must be a finite number in [{low}, {high}]")
    return int(value) if integral else float(value)


def parse_drivers(raw: Any, queues: Mapping[str, Any]) -> Mapping[str, Driver]:
    """Validate every declared driver; refuse the whole set on any error."""
    if not isinstance(raw, Mapping):
        raise ConfigError("drivers must be a table")
    result = {}
    for name, spec in raw.items():
        where = f"drivers.{name}"
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ConfigError(f"{where} must be a lowercase identifier")
        if not isinstance(spec, Mapping):
            raise ConfigError(f"{where} must be a table")
        unknown = set(spec) - _DRIVER_KEYS
        if unknown:
            raise ConfigError(f"Unknown keys in {where}: {', '.join(sorted(unknown))}")
        command = spec.get("command")
        if not isinstance(command, list) or not command:
            raise ConfigError(f"{where}.command must be a non-empty argv array")
        if any(not isinstance(arg, str) or not arg or "\n" in arg or "\x00" in arg
               for arg in command):
            raise ConfigError(f"{where}.command must hold nonempty strings without newlines or NUL")
        timeout = _number(spec.get("timeout_seconds", TIMEOUT_DEFAULT),
                          f"{where}.timeout_seconds", TIMEOUT_MIN, TIMEOUT_MAX)
        maximum = _number(spec.get("max_bytes", MAX_BYTES_DEFAULT),
                          f"{where}.max_bytes", MAX_BYTES_MIN, MAX_BYTES_MAX, integral=True)
        diagnostic = spec.get("diagnostic_bytes", DIAGNOSTIC_BYTES_DEFAULT)
        if type(diagnostic) is not int or not 0 <= diagnostic <= ADAPTER_DIAGNOSTIC_BYTES_MAX:
            raise ConfigError(f"{where}.diagnostic_bytes must be an integer in "
                              f"[0, {ADAPTER_DIAGNOSTIC_BYTES_MAX}]")
        queue = spec.get("observation_queue")
        if (not isinstance(queue, str) or queue not in queues
                or not allows(queues[queue].write, "host")):
            raise ConfigError(f"{where}.observation_queue must name a declared queue that host may write")
        result[name] = Driver(name, tuple(command), timeout, maximum, queue, diagnostic)
    return MappingProxyType(result)


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(key) or _has_nul(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nul(item) for item in value)
    return False


def _no_duplicates(pairs: list) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("Duplicate key")
        seen[key] = value
    return seen


def _loads(raw: str) -> Any:
    return loads_finite(raw, object_pairs_hook=_no_duplicates)


def invoke(driver: Driver, request: dict, *, cancel=lambda: False) -> dict:
    """Run one configured program once and return its external-untrusted object.

    ``request`` is a plain dict; ``cancel`` is polled by the bounded runner. A
    successful response is any JSON object, returned verbatim. All failures
    raise ``Denied``; a normally exited nonzero program may attach a bounded,
    labelled stderr diagnostic.
    """
    if not isinstance(request, dict):
        raise Denied("Observation driver request must be a dict")
    if _has_nul(request):
        raise Denied("Observation driver request must not contain NUL")
    try:
        payload = canonical(request)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise Denied("Observation driver request is not JSON-serializable") from exc
    if len(payload) > driver.max_bytes:
        raise Denied("Observation driver request exceeds byte limit")
    result = run(list(driver.command), timeout=driver.timeout_seconds,
                 maximum=driver.max_bytes, input_data=payload + b"\n", cancel=cancel)
    if result.reason == "timeout":
        raise Denied("Observation driver timed out")
    if result.reason == "output_limit":
        raise Denied("Observation driver response exceeds byte limit")
    if result.exit_code != 0 or result.reason != "exited":
        diagnostic = adapter_failure_diagnostic(
            result, source="driver", maximum_bytes=driver.diagnostic_bytes)
        raise Denied(f"Observation driver failed ({result.reason}){diagnostic}")
    try:
        response = _loads(result.stdout)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise Denied("Observation driver must return a JSON object") from exc
    if not isinstance(response, dict):
        raise Denied("Observation driver must return a JSON object")
    return response


def record_observations(store: Any, driver: Driver, response: dict) -> dict:
    """Append a response's observations to the declared queue as ``host``.

    Observation bodies are opaque to the kernel: the queue's own schema and
    body bound validate each one. Appends are sequential and not transactional;
    a refusal raises ``Denied`` and reports no success receipt. The returned
    dict has no meaning beyond the count appended.
    """
    if not isinstance(response, dict):
        raise Denied("Observation driver response must be a dict")
    items = response.get("observations")
    if not isinstance(items, list):
        raise Denied("Observation driver response must carry a list of observations")
    if len(items) > MAX_OBSERVATIONS:
        raise Denied("Observation driver response carries too many observations")
    appended = 0
    for body in items:
        if type(body) is not dict:
            raise Denied("Each observation must be a plain object")
        store.append(driver.observation_queue, body, "host")
        appended += 1
    return {"appended": appended}
