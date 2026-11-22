"""Merge persisted VCS poll state into daemon in-memory state.

Owns the bounded, fail-safe refresh of timestamps, cursors and bounded CI
failure/success maps from the shared ``vcs-poll.json``. Host-only state; never
model input. No adapter calls or evidence writes happen here.
"""
from __future__ import annotations

from .ci_observation import _clean_ci_failures
from .ci_polling import _merge_ci_success_maps
from .vcs_proposal_poll import _valid_proposal_cursor


def _wall(value):
    """Wall-clock timestamp or None for poll state merging."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def refresh_poll_state(state: dict, shared: dict, config=None, *, refresh_ci=True) -> dict:
    """Refresh in-memory poll state from shared persisted state.

    Schema: ``state`` is daemon in-memory poll state (fetch, CI, proposal
    timestamp/cursor); ``shared`` is the flat ``vcs-poll.json`` mapping.
    Returns the ``ci`` sub-state. Bounded proposal ref and complete-message-id
    maps are also refreshed. A valid shared CI failure map is
    also authoritative and replaces stale in-memory failures, including when
    empty. An absent, unreadable, or invalid failure map leaves the local map
    intact. Success evidence has its separate merge policy. Bounds: timestamps
    must be numeric. Trust: local
    operator state, never model input. ``refresh_ci=False`` defers cache
    normalization until after upstream refresh/live-ref discovery. Failure: never raises.
    """
    ci_state = state.setdefault("ci", {})
    if not isinstance(shared, dict):
        return ci_state
    for key, target in (("last_fetch", state), ("last_poll", ci_state),
                        ("last_proposal_poll", state), ("last_ci_wait_poll", state)):
        seen = _wall(shared.get(key))
        if seen is None:
            continue
        current = _wall(target.get(key))
        if current is None or seen > current:
            target[key] = seen
    if isinstance(shared.get("ci_wait_cursor"), (str, list)):
        state["ci_wait_cursor"] = shared["ci_wait_cursor"]
    # Copy even corrupt stream state: the CI-wait controller must recover by
    # full reads, not silently retain an apparently caught-up local cursor.
    for key in ("ci_changes_cursor", "ci_changes_pending", "ci_changes_epoch",
                "ci_wait_invalidation"):
        if key in shared:
            state[key] = shared[key]
    if isinstance(shared.get("proposal_refs"), dict):
        state["proposal_refs"] = shared["proposal_refs"]
    if isinstance(shared.get("proposal_complete_messages"), dict):
        state["proposal_complete_messages"] = shared["proposal_complete_messages"]
    if "proposal_cursor" in shared:
        stored_cursor = shared.get("proposal_cursor")
        if stored_cursor is None:
            state["proposal_cursor"] = None
        else:
            valid_cursor = _valid_proposal_cursor(stored_cursor)
            if valid_cursor is not None:
                state["proposal_cursor"] = valid_cursor
    if refresh_ci:
        _refresh_ci_cache(ci_state, shared, config)
    return ci_state


def _refresh_ci_cache(ci_state, shared, config=None, *, live_branches=None,
                      defer_branch_cap=False):
    """Refresh bounded CI facts only after any known-deleted refs are excluded."""
    if not isinstance(shared, dict):
        return
    if isinstance(shared.get("failures"), dict):
        ci_state["failures"] = _clean_ci_failures(
            shared["failures"], config, live_branches=live_branches,
            defer_branch_cap=defer_branch_cap)
    ci_state["successes"] = _merge_ci_success_maps(
        ci_state.get("successes"), shared.get("successes"), config,
        live_branches=live_branches, defer_branch_cap=defer_branch_cap)
