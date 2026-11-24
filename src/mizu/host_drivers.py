"""Host invocation seam for configured kernel observation drivers.

The kernel stays generic: it owns the bounded request/response mechanism and
the queue append, but calls no host entry point. A trusted host caller resolves
an operator-declared driver by name and delegates to the kernel; all bounds,
trust decisions and failure rules remain there. This seam adds no command of
its own and changes no dedicated host authority.
"""
from __future__ import annotations

from .errors import Denied


def observe(config, project, driver_name, request: dict) -> dict:
    """Invoke one declared driver and return its receipt and observations.

    Schema: ``request`` is a plain JSON object; the result is
    ``{"appended": int, "observations": [plain dicts from the response]}``.
    Bounds and retry: entirely the kernel's, one call
    with no automatic replay. Trust: the driver is operator configuration and
    the request comes from a trusted host caller; the response stays
    external-untrusted until the declared queue schema validates each body.
    Failure: ``Denied`` for an unknown driver, a non-dict request or any
    kernel refusal; no success receipt is returned.
    """
    from .kernel import invoke, record_observations
    if not isinstance(driver_name, str):
        raise Denied("Unknown observation driver")
    driver = config.drivers.get(driver_name)
    if driver is None:
        raise Denied(f"Unknown observation driver: {driver_name}")
    if not isinstance(request, dict):
        raise Denied("Observation driver request must be a dict")
    response = invoke(driver, request)
    receipt = record_observations(project.kernel_queues, driver, response)
    return {**receipt, "observations": [dict(body) for body in response["observations"]]}
