"""Private handback preflight; unsupported execution substrates stay reserved.

No integrated launch owner currently enforces a durable no-new-launch fence or
observes supervisor settlement. Even an empty Linux cgroup and absent containers
are only point-in-time observations without that fence. Host callbacks cannot
supply the missing authority. This boundary deliberately executes no callbacks,
cleanup commands or handback until Mizu owns that lifecycle contract.
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
