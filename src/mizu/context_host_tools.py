"""Context-side host sync and report handlers.

``_op_sync`` refreshes trusted upstream refs and records the receipt;
``_op_report`` stages a role's commentary artifact. Both are mixed back into
``Context`` so the ``sync`` and ``report`` dispatch names and their checks are
unchanged. No behaviour, grant, default or seam changes.
"""
from __future__ import annotations

from . import vcs as _vcs
from .errors import Denied
from .fs import write_json
from .vcs_poll_policy import vcs_default_branch


class ContextHostToolsMixin:
    def _op_sync(self, args: dict) -> dict:
        # Host-side refresh only: fetch upstream refs via the trusted adapter
        # and inject them read-only into the harness store (virtual
        # refs/remotes/upstream/*, never inside the working tree). Merging is
        # worker policy (workspace edits + verify + publish); the mechanism
        # only records the receipt. Requires a writable workspace so read
        # roles cannot mutate the shared ref view.
        if self.role.workspace != "write":
            raise Denied("Sync requires a writable workspace")
        try:
            fetched = _vcs.fetch_refs(self.config.vcs, cancel=self.tools_cancelled)
        except OSError as exc:
            raise Denied(f"Upstream sync is unavailable: {exc}") from exc
        try:
            receipt = _vcs.inject_refs(self.project.root, fetched["refs"])
        except OSError as exc:
            raise Denied(f"Upstream refs are unavailable: {exc}") from exc
        names = sorted(fetched["refs"])
        record = {"synced": True, "injected": receipt["injected"],
                  "prefix": receipt["prefix"], "trust": "external-untrusted",
                  "upstream": fetched["refs"].get(vcs_default_branch(self.config)),
                  "evidence": "sync.json", "refs": names}
        write_json(self.run_dir / "sync.json",
                   {"injected": receipt["injected"], "prefix": receipt["prefix"],
                    "trust": "external-untrusted", "refs": fetched["refs"]})
        return record

    def _op_report(self, args: dict) -> dict:
        self.commentary = args
        write_json(self.run_dir / "commentary.json", args)
        return {"staged": True}

