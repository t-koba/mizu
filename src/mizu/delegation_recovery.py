"""Private callback-only handback; no runner, launch or host commands.

The trusted host serializes recovery for this exact binding, establishes a
one-shot launch fence and settles the supervisor BEFORE calling, and keeps that
fence through handback (including after interruption). The attestations below
are host assertions, not OS observations or launch authority. A retry reinspects
all resources; a completed/uncertain handback is resolved via store status.
"""
from copy import deepcopy

from .delegation_jobs import MAX_RESULT_BYTES, _bound, _text
from .delegation_scope import _scope_empty
from .errors import Denied
from .fs import canonical


def _recover_quiescent(store, job_id, token, result, *, binding, attest_launch_fence,
                       attest_supervisor_settled, query, reconcile_container, maximum):
    """Prove saved Linux scope + OCI inventory absent, then submit handback.

    attest_launch_fence and attest_supervisor_settled are trusted host
    callbacks. Each receives a defensive copy of the validated saved binding
    and must return literal True: the fence attests the host's one-shot launch
    fence for that exact binding is established and held; the supervisor
    attests launches for that exact binding have settled. Both run only after
    the current active token, exact binding and terminal report are validated,
    and before any systemd/OCI callback. Stale, mismatched or unobservable
    attestation evidence (non-True or raising) refuses before query/cleanup.

    query is the bounded read-only systemd callback consumed by _scope_empty;
    maximum bounds its output and each cgroup read. Timeouts belong to the host.
    reconcile_container receives a copy of each exact saved {name, id, uid}; only
    literal True attests positive absence after any cleanup. It must inspect BOTH
    selectors in the existing default rootless-Podman namespace for that uid,
    refuse renamed/mismatched/unobservable resources, and never substitute a
    runtime/store. The host owns bounded inspection/removal and retry safety.

    No callbacks run without the current active token/binding and terminal
    report. Unknown/live proof or interrupted cleanup never releases the
    reservation. Cleanup attempts continue across the inventory even if the
    scope or another container is unknown; no partial proof is saved. The
    ledger independently rechecks identity and all gates at final commit.
    """
    _text(result, MAX_RESULT_BYTES)
    if (not callable(attest_launch_fence) or not callable(attest_supervisor_settled)
            or not callable(query) or not callable(reconcile_container)):
        raise Denied('Delegation recovery callbacks required')
    with store._locked():
        job = store._runner(store._load(), job_id, token)
        if job['execution'] is None:
            raise Denied('Delegation execution is not terminal')
        saved = _bound(job)
        if canonical(binding) != canonical(saved):
            raise Denied('Delegation recovery binding mismatched')

    for attest, label in ((attest_launch_fence, 'launch fence'),
                          (attest_supervisor_settled, 'settled supervisor')):
        try:
            verdict = attest(deepcopy(saved))
        except Denied:
            raise
        except Exception as error:
            raise Denied(
                f'Delegation recovery requires a fenced launch and settled supervisor ({label} unobservable)'
            ) from error
        if verdict is not True:
            raise Denied(
                f'Delegation recovery requires a fenced launch and settled supervisor ({label} unproven)')

    absent, errors = [], []
    try:
        scope_clean = _scope_empty(saved, query=query, maximum=maximum)
    finally:
        for container in saved['containers']:
            try:
                if reconcile_container(deepcopy(container)) is not True:
                    raise Denied('Delegation container absence unproven')
                absent.append(container['id'])
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]
    if scope_clean is not True:
        raise Denied('Delegation scope is not empty')
    store.runner_quiescent(job_id, token, result, cleanup={
        'binding': saved, 'scope_clean': True, 'containers_absent': absent})
