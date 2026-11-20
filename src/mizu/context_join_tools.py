"""Context nested-join dispatch: consultation and delegation join lifecycle.

These two methods own the Context side of nested consultation and delegation:
they gate the join on its injected callback, mark the join active through the
Context activity accounting and restore the idle signal on exit. They are
mixed back into ``Context`` and referenced by its dispatch table, so every
``Context.method`` reference and ``patch.object(Context, ...)`` call site keeps
resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from .errors import Denied


class ContextJoinToolsMixin:
    def _op_consult(self, args: dict) -> dict:
        if self.consult is None:
            raise Denied("Nested consultation is disabled")
        self.joins_idle.clear()
        self._activity_change(lambda: setattr(self, "_activity_join", "consult"), count="join:consult")
        try:
            return self.consult(self, args)
        finally:
            self._activity_change(lambda: setattr(self, "_activity_join", None))
            self.joins_idle.set()

    def _op_delegate(self, args: dict) -> dict:
        if self.delegate is None:
            raise Denied("Nested delegation is disabled")
        self.joins_idle.clear()
        self._activity_change(lambda: setattr(self, "_activity_join", "delegate"), count="join:delegate")
        try:
            return self.delegate(self, args)
        finally:
            self._activity_change(lambda: setattr(self, "_activity_join", None))
            self.joins_idle.set()
