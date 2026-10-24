"""Bounded proposal-page polling and opaque cursor validation."""
from __future__ import annotations

from .errors import Denied
from . import vcs as _vcs
from .fs import now as now_iso
from .vcs_poll_policy import (capped_vcs_settings, upstream_fetch_due,
                              vcs_poll_proposals_enabled,
                              vcs_poll_status_timeout)

#: Opaque adapter pagination tokens are persisted for resumption, but never
#: rendered into events or prompts. The VCS schema fixes this bound.
MAX_PROPOSAL_CURSOR = 256


def _valid_proposal_cursor(value) -> str | None:
    """Return a usable opaque proposal cursor, else None (never raises)."""
    try:
        if not isinstance(value, str) or not value or len(value) > MAX_PROPOSAL_CURSOR:
            return None
        _vcs._reject_terminal_controls(value, "Invalid VCS proposals cursor")
        return value
    except Denied:
        return None


def poll_proposals(config, project, state: dict, *, now: float,
                   interval: float, reader=None) -> dict | None:
    """Observe at most one external-proposal page on each opted-in tick.

    Schema: state stores ``last_proposal_poll`` and an opaque
    ``proposal_cursor`` across ticks. Bounds: one normalized proposal page
    (at most 256 rows) per due tick, using the existing status timeout cap.
    Trust: host-side observation only; validated rows are recorded through
    ``vcs.record_proposal_state`` and never imply state for missing rows.
    Retry/cancellation: no same-tick retries; the timestamp advances on all
    failures, while the cursor advances only after the entire page is
    recorded successfully. Evidence: one bounded ``proposal_poll`` event.
    Failure: adapter, malformed-page, or recording errors are visible as
    ``ok=False``; a failed page preserves its input cursor for retry.
    """
    if not vcs_poll_proposals_enabled(config):
        return None
    if not upstream_fetch_due(state.get("last_proposal_poll"), now, interval):
        return None
    state["last_proposal_poll"] = now
    cursor = _valid_proposal_cursor(state.get("proposal_cursor"))
    params = {"cursor": cursor} if cursor is not None else {}

    def _default_reader(settings, op, request):
        return _vcs.read_via(
            capped_vcs_settings(settings, vcs_poll_status_timeout(config)),
            op, request)

    do_read = reader if reader is not None else _default_reader
    observed = 0
    recorded = 0
    changed = 0
    try:
        data = do_read(config.vcs, "proposals", params)
        if not isinstance(data, dict):
            raise Denied("VCS proposals response must be an object")
        rows = data.get("proposals")
        complete = data.get("complete")
        next_cursor = data.get("cursor")
        if not isinstance(rows, list) or len(rows) > _vcs.MAX_PROPOSALS:
            raise Denied("VCS proposals response must carry a bounded proposals array")
        if complete is not None and not isinstance(complete, bool):
            raise Denied("Invalid VCS proposals completeness flag")
        if next_cursor is not None and _valid_proposal_cursor(next_cursor) is None:
            raise Denied("Invalid VCS proposals cursor")
        if complete is False and next_cursor is None:
            raise Denied("Incomplete VCS proposals page must carry a cursor")
        if complete is False and next_cursor == cursor:
            raise Denied("Incomplete VCS proposals cursor did not advance")

        observed = len(rows)
        for row in rows:
            receipt = _vcs.record_proposal_state(project, row, origin="daemon")
            recorded += 1
            if receipt.get("changed") is True:
                changed += 1

        # Do not move/clear the cursor until every validated row has been
        # recorded. Re-reading a partially committed page is safe because
        # record_proposal_state has stable, change-gated deduplication.
        if complete is True:
            state["proposal_cursor"] = None
        elif next_cursor is not None:
            state["proposal_cursor"] = next_cursor
        else:
            # No completeness/cursor metadata is the legacy, non-paginated
            # response shape; it represents one complete enumeration.
            state["proposal_cursor"] = None
        state["proposal_last_error"] = ""
        return {"event": "proposal_poll", "ok": True,
                "observed": observed, "recorded": recorded,
                "changed": changed,
                "complete": complete,
                "has_cursor": state.get("proposal_cursor") is not None,
                "trust": "external-untrusted", "time": now_iso()}
    except (Denied, OSError, ValueError, TypeError) as exc:
        message = str(exc) or type(exc).__name__
        message = message[:512]
        state["proposal_last_error"] = message
        return {"event": "proposal_poll", "ok": False,
                "observed": observed, "recorded": recorded,
                "error": message, "trust": "external-untrusted",
                "time": now_iso()}
