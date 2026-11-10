"""Private handback preflight; unsupported execution substrates stay reserved.

The ledger claim returns before external creation/start. A caller paused after
that return can launch after cleanup observes the saved unit and containers
absent. Terminal execution reports block another claim, not that outstanding
launch; the prepared recovery bit likewise cannot settle an in-flight create.
The saved scope does not bind that outer launcher. Ledger locks and bounded
systemd/Podman cleanup cannot settle it. No callbacks, cleanup or release proof
are permitted from point-in-time absence or caller assertions.
"""
from .delegation_jobs import MAX_RESULT_BYTES, _bound, _text
from .errors import Denied
from .fs import canonical


def _recover_quiescent(store, job_id, token, result, *, binding, attest_launch_fence=None,
                       attest_supervisor_settled=None, query=None, reconcile_container=None, maximum=None):
    """Validate persisted identity, then refuse unsupported post-execution recovery.

    The former callback arguments are not proof and are never invoked, including
    on retry. No partial observation is persisted. The reservation survives a
    missing/stale binding, interrupted caller or fabricated positive assertions.
    There is no supported substrate yet; runner integration remains disabled.
    """
    _text(result, MAX_RESULT_BYTES)
    with store._locked():
        job = store._runner(store._load(), job_id, token)
        if job['execution'] is None:
            raise Denied('Delegation execution is not terminal')
        if canonical(binding) != canonical(_bound(job)):
            raise Denied('Delegation recovery binding mismatched')
        raise Denied('Delegation handback unsupported: no Mizu-owned launch fence and supervisor settlement')
