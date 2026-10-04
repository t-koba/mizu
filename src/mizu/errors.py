"""Errors with stable, shell-friendly exit codes."""


class MizuError(Exception):
    code = 1


class ConfigError(MizuError):
    code = 78


class Busy(MizuError):
    code = 75


class Denied(MizuError):
    code = 77


class LimitExceeded(MizuError):
    code = 69


class InfraExceeded(LimitExceeded):
    """Deferrable infrastructure limit: daily budget or disk reserve.

    A subclass of ``LimitExceeded`` so existing ``except LimitExceeded``
    admission handling still catches it, while the consecutive-failure
    brake can distinguish it from model/local bound faults by type alone
    (no message parsing). Only budget/disk guards raise this; engine
    deadlines, event-stream bounds, RPC deadlines, per-run tool/request
    bounds, file-count/snapshot bounds stay plain ``LimitExceeded`` and
    still count toward the brake.
    """


class Cancelled(MizuError):
    code = 130


class ProtocolError(MizuError):
    code = 76


class ModelFailure(ProtocolError):
    """A reported engine/model execution failure, distinct from local invariants.

    Only fields supplied by the adapter are retained; message text is never
    parsed to invent status codes or recovery times. No credentials/headers.
    """
    def __init__(self, source, *, kind=None, code=None, message='', retry_at=None, details=None):
        import math
        self.evidence = {'source': str(source)[:64],
                         'kind': str(kind)[:128] if kind is not None else None,
                         'code': code if type(code) in (str, int) else None,
                         'message': str(message)[:4000], 'retry_at': None}
        if isinstance(self.evidence['code'], str):
            self.evidence['code'] = self.evidence['code'][:256]
        if type(retry_at) in (int, float) and 0 <= retry_at <= 253402300799 and math.isfinite(retry_at):
            self.evidence['retry_at'] = retry_at
        if isinstance(details, dict):
            from .fs import canonical
            try:
                if len(canonical(details)) <= 16384:
                    self.evidence['details'] = details
            except (TypeError, ValueError, RecursionError):
                pass
        super().__init__(self.evidence['message'])
