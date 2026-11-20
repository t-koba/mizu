"""Optional context sections of the unit prompt and which role receives which.

Schema: every key of the unit-opening JSON object belongs to exactly one named
section (the table ``SECTIONS``) or is a fixed key that every role receives
(``FIXED_KEYS``: the unit statement, tool surface, snapshot pins, and every
key that delivers something pending to the role: work-item events, CI waits,
and the fixed ci_branch configuration and external_check_wake facts and
capability-gated details route). The ci
section carries only aggregate summaries and is strictly opt-in via include.
Default membership is one uniform rule: a section is offered when the role holds a
capability that reads or acts on it. Sections whose projection is already
empty without that capability keep their (empty) keys, so a role's prompt only
changes where content was being sent that the role could not use. Policy:
``roles.<name>.context_sections = {include = [...], exclude = [...]}``
restores or removes optional sections by name; unknown or fixed names, and a
name in both lists, are refused at configuration load. Bounds: at most the
number of defined sections per list. Trust: operator configuration. Failure:
``ConfigError`` at load; nothing here reads state at prompt time.
"""
from __future__ import annotations

import dataclasses

WORK_CAPABILITIES = frozenset({"work_items_read", "work_items_create", "work_items_transition", "work_items_reopen",
                               "work_order"})


@dataclasses.dataclass(frozen=True)
class Section:
    keys: tuple[str, ...]
    #: Capabilities that read or act on the section's content.
    capabilities: frozenset
    #: Whether the section stays (as its empty projection) when none is held.
    #: False only where the projection carries content regardless of capability.
    kept_without_capability: bool


SECTIONS: dict[str, Section] = {
    "open_work_items": Section(("open_work_items",), WORK_CAPABILITIES, False),
    "ready_work": Section(("ready_work",), WORK_CAPABILITIES, False),
    "work_order": Section(("work_order",), frozenset({"work_order"}), True),
    "check_jobs": Section(("check_jobs", "check_continuation", "shared_checks", "check_discovery"),
                          frozenset({"check_job"}), True),
    "queues": Section(("queues",), frozenset({"queues"}), False),
    "research": Section(("research_state",), frozenset({"research_read", "research"}), True),
    "ci": Section(("ci_summary",), frozenset(), False),
    "acceptance": Section(("acceptance_commands",), frozenset({"verify"}), True),
    "report": Section(("previous_report",), frozenset({"report"}), True),
}

#: Keys of the opening that are never optional.
FIXED_KEYS = frozenset({
    "unit", "unit_tool_surface", "goal_digest", "admission", "grant_transition",
    "published_snapshot", "recent_snapshots", "snapshot_delta", "workspace", "workspace_mode",
    "work_item_events", "ci_wait_delivery",
    "capture_limit", "review_authority", "publication_state", "ci_branch", "external_check_wake",
    "run", "now", "timezone"})

#: Row-fingerprint groups kept for session cursors, by the section that shows them.
ROW_GROUPS = {"open_work_items": ("open_work_items",), "check_jobs": ("check_jobs", "shared_checks")}


def validate(name: str, value) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (include, exclude) for one role's ``context_sections`` table."""
    from .errors import ConfigError
    if not isinstance(value, dict) or set(value) - {"include", "exclude"}:
        raise ConfigError(f"roles.{name}.context_sections must be a table with include/exclude lists")
    lists = {}
    for key in ("include", "exclude"):
        items = value.get(key, [])
        if (not isinstance(items, list) or len(items) > len(SECTIONS)
                or any(not isinstance(item, str) for item in items) or len(set(items)) != len(items)):
            raise ConfigError(f"roles.{name}.context_sections.{key} must be a list of unique section names")
        for item in items:
            if item not in SECTIONS:
                raise ConfigError(f"roles.{name}.context_sections.{key}: unknown or fixed section '{item}'; "
                                  f"optional sections are {', '.join(sorted(SECTIONS))}")
        lists[key] = tuple(items)
    if set(lists["include"]) & set(lists["exclude"]):
        raise ConfigError(f"roles.{name}.context_sections lists a section in both include and exclude")
    return lists["include"], lists["exclude"]


def included(role) -> frozenset:
    """Optional sections this role's unit prompt carries."""
    caps = set(role.capabilities)
    names = {name for name, section in SECTIONS.items()
             if section.kept_without_capability or section.capabilities & caps}
    names |= set(role.context_include)
    names -= set(role.context_exclude)
    return frozenset(names)


def shape(context, payload: dict) -> dict:
    """Drop the keys of sections this role does not receive (order preserved)."""
    keep = included(context.role)
    drop = {key for name, section in SECTIONS.items() if name not in keep for key in section.keys}
    fingerprints = getattr(context, "prompt_row_fingerprints", None)
    if isinstance(fingerprints, dict):
        # Rows that were not shown must not count as shown to a later delta.
        for name, groups in ROW_GROUPS.items():
            if name not in keep:
                for group in groups:
                    if group in fingerprints:
                        fingerprints[group] = {}
    return {key: value for key, value in payload.items() if key not in drop}
