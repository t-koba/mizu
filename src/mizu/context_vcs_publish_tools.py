"""Context-side VCS publication handler.

The writable-workspace gate, approval checks, source projection, durable
publication identity and evidence observation stay in one handler. It is mixed
back into ``Context`` so the ``vcs_publish`` dispatch name and grant checks
are unchanged; call-side patch targets follow the handler's new owner. No
behaviour, grant or default changes.
"""
from __future__ import annotations

from . import reviews
from . import vcs as _vcs
from .errors import Denied
from .fs import lock
from .publication import Publication, source_commit


class ContextVcsPublishMixin:
    def _op_vcs_publish(self, args: dict) -> dict:
        # External publication: push/PR via the trusted adapter behind a
        # recorded human GO approval bound to branch and code digest.
        # Fail-closed without approval; stale digests refused. Merging stays
        # workspace edits + verify; this only ships the approved tree.
        if self.role.workspace != "write":
            raise Denied("Publication requires a writable workspace")
        self.close_exec()
        captured = self.project.snapshots.capture_files(self.workspace)
        if captured.get("skipped"):
            raise Denied("Publication requires a representable snapshot")
        code_digest = captured["code_digest"]
        from .distribution import public_source_identity
        try:
            projection = public_source_identity(captured.get("files", {}))
        except ValueError as exc:
            raise Denied(f"Publication requires a projectable snapshot: {exc}") from exc
        approval = _vcs.require_go_approval(self.project, args["branch"], code_digest,
                                            queue=self.config.vcs.get("approval_queue"))
        review_approvals = reviews.require_approvals(self, code_digest)
        try:
            source = source_commit(
                self.workspace, timeout=self.config.publication.source_commit_timeout_seconds,
                cancel=self.tools_cancelled)
            publication = Publication(self.project.root, source, code_digest,
                                      args["branch"], args["op"], projection=projection)
            with lock(publication.lock):
                intent, receipt = publication.prepare(approval["approval"], self.run_dir.name)
                before_local, observe = self._mutation_evidence(
                    "vcs-publish", args["op"],
                    {"branch": args["branch"], "code_digest": code_digest,
                     "projection_schema": projection["schema"],
                     "projection_algorithm": projection["algorithm"],
                     "projection_digest": projection["digest"],
                     "approval": approval["approval"], "source_commit": source,
                     "publication_id": publication.id})

                def before_invoke():
                    before_local()
                    publication.mark_dispatch()

                if receipt is None:
                    try:
                        data = _vcs.publish_via(
                            self.project.vcs_settings(self.config), args["op"],
                            {**publication.identity, "publication_id": publication.id},
                            cancel=self.tools_cancelled, before_invoke=before_invoke)
                    except (Denied, OSError) as exc:
                        raise Denied(f"VCS publication {publication.id}: {exc}") from exc
                    receipt = publication.complete(intent, data["published_sha"], data.get("lineage"))
                else:
                    # Retain a run-local observation even on a durable replay.
                    before_local()
                    data = {"digest": code_digest, "publication_id": publication.id,
                            "projection_schema": projection["schema"],
                            "projection_algorithm": projection["algorithm"],
                            "projection_digest": projection["digest"],
                            "published_sha": receipt["published_sha"],
                            "trust": "external-untrusted"}
        except OSError as exc:
            raise Denied(f"VCS publication is unavailable: {exc}") from exc
        record = {**receipt, "published": True, "review": list(review_approvals),
                  "receipt": publication.path("receipt").relative_to(self.project.root).as_posix(),
                  "trust": "external-untrusted", "result": data}
        return observe(record)
