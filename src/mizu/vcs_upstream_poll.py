"""Bounded upstream ref refresh and periodic fetch polling."""
from __future__ import annotations

from .errors import Denied
from . import vcs as _vcs
from .fs import now as now_iso
from .vcs_poll_policy import (capped_vcs_settings, upstream_fetch_due,
                              vcs_poll_fetch_timeout)


def refresh_upstream(config, project) -> dict:
    """Host-side upstream refresh: fetch refs and inject them read-only.

    Schema: returns ``{"injected": n, "prefix": ..., "trust": ...}``.
    Bounds: adapter bounds from ``config.vcs``; at most 4096 refs.
    Trust: operator-owned adapter; refs stay external-untrusted.
    Failure: ``Denied`` on adapter/shape errors; ``OSError`` on host I/O.
    No capability check here; the ``sync`` tool adds the grant gate.
    """
    fetched = _vcs.fetch_refs(capped_vcs_settings(config.vcs, vcs_poll_fetch_timeout(config)))
    receipt = _vcs.inject_refs(project.root, fetched["refs"])
    return {"injected": receipt["injected"], "prefix": receipt["prefix"],
            "trust": "external-untrusted", "refs": dict(fetched["refs"])}


def poll_upstream(config, project, state: dict, *, now: float, interval: float,
                  refresh=None) -> dict | None:
    """Best-effort periodic upstream refresh for the daemon loop.

    Schema: ``state`` holds ``last_fetch`` (wall-clock) across calls; returns
    an ``upstream_fetch`` event dict or ``None`` when skipped (disabled or not
    due). Bounds: one ``refresh`` invocation per due poll under the adapter
    timeout. Trust: host side only via ``refresh_upstream``; refs stay
    external-untrusted. Retry/cancellation: no retry; ``last_fetch`` advances
    on failure too so one bad adapter cannot busy-loop. Evidence: the
    returned event names ok/injected or error; this helper never publishes a
    snapshot (injected refs are digest-excluded). Failure: ``Denied``,
    ``OSError`` and ``ValueError`` become ``ok=False`` events, never raised.
    """
    if not config.vcs.get("command"):
        return None
    if not upstream_fetch_due(state.get("last_fetch"), now, interval):
        return None
    state["last_fetch"] = now
    do_refresh = refresh if refresh is not None else refresh_upstream
    try:
        receipt = do_refresh(config, project)
    except (Denied, OSError, ValueError) as exc:
        state["last_error"] = str(exc)
        return {"event": "upstream_fetch", "ok": False,
                "error": str(exc), "time": now_iso()}
    state["last_error"] = ""
    state["last_ok"] = now
    return {"event": "upstream_fetch", "ok": True,
            "injected": receipt["injected"], "prefix": receipt["prefix"],
            "trust": "external-untrusted", "time": now_iso()}
