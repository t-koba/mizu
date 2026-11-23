"""Pure child-admission decision for bounded fan-out.

This module decides, never launches. A caller supplies the configured child
slot ceiling, the shared request/tool budgets and the already committed usage,
together with an ordered list of per-child cost estimates. The decision admits
only the leading prefix that fits every limit, so a refused child can never be
overtaken by a later one. Every refusal names the binding limit.

No resource is acquired, no budget is consumed, no child is started and no
capability is granted here. Current synchronous and in-process async
delegation remain unchanged; this is a mechanism for a future admitted caller.
"""
from __future__ import annotations

from .errors import Denied, LimitExceeded

MAX_CHILD_BATCH = 64
MAX_BUDGET = 1000000

_RESOURCES = ('slots', 'requests', 'tools')


def _count(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_BUDGET:
        raise Denied(f"Child admission has an invalid {name}")
    return value


def _costs(costs: object) -> list[tuple[int, int]]:
    if not isinstance(costs, (list, tuple)):
        raise Denied("Child admission batch is not a cost list")
    if len(costs) > MAX_CHILD_BATCH:
        raise LimitExceeded("Child admission batch exceeds its bound")
    parsed = []
    for index, cost in enumerate(costs):
        if not isinstance(cost, (list, tuple)) or len(cost) != 2:
            raise Denied(f"Child admission cost {index} is not a request/tool pair")
        parsed.append((_count(cost[0], f"cost {index} requests"),
                       _count(cost[1], f"cost {index} tools")))
    return parsed


def admit_children(*, max_slots: object, active_slots: object,
                   request_budget: object, used_requests: object,
                   tool_budget: object, used_tools: object,
                   costs: object) -> dict:
    """Decide which leading children fit; return explicit refusals for the rest.

    The caller's usage must be consistent with its ceilings. A consistent
    state with exhausted capacity is an ordinary refusal, not a refusal to
    answer. Inputs are bounded and validated before any decision is returned,
    so an incomplete or malformed observation can never look like admission.
    """
    ceiling = {
        'slots': _count(max_slots, 'slot ceiling'),
        'requests': _count(request_budget, 'request budget'),
        'tools': _count(tool_budget, 'tool budget'),
    }
    used = {
        'slots': _count(active_slots, 'active slots'),
        'requests': _count(used_requests, 'used requests'),
        'tools': _count(used_tools, 'used tools'),
    }
    parsed = _costs(costs)
    consistent = all(used[name] <= ceiling[name] for name in _RESOURCES)
    remaining = ({name: ceiling[name] - used[name] for name in _RESOURCES}
                 if consistent else {name: 0 for name in _RESOURCES})
    admitted: list[int] = []
    refused: list[dict] = []
    reserved = {name: 0 for name in _RESOURCES}
    blocked_by: int | None = None
    for index, (requests, tools) in enumerate(parsed):
        if not consistent:
            refused.append({'index': index, 'reasons': ['state-inconsistent']})
            continue
        if blocked_by is not None:
            refused.append({'index': index, 'reasons': ['earlier-child-refused'],
                            'blocked_by': blocked_by})
            continue
        reasons = []
        if remaining['slots'] < 1:
            reasons.append('slot-limit')
        if remaining['requests'] < requests:
            reasons.append('request-budget')
        if remaining['tools'] < tools:
            reasons.append('tool-budget')
        if reasons:
            blocked_by = index
            refused.append({'index': index, 'reasons': reasons})
            continue
        admitted.append(index)
        remaining['slots'] -= 1
        remaining['requests'] -= requests
        remaining['tools'] -= tools
        reserved['slots'] += 1
        reserved['requests'] += requests
        reserved['tools'] += tools
    return {
        'admitted': admitted,
        'refused': refused,
        'reserved': reserved,
        'remaining': remaining,
        'consistent': consistent,
    }
