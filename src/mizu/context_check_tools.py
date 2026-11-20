"""Context check-job and verification dispatch: thin wrappers over their owners.

These two methods own the Context side of check-job operations and of the
verification tool call, including the queued-verify branch. They are mixed back
into ``Context`` and referenced by its dispatch table, so every
``Context.method`` reference and ``patch.object(Context, ...)`` call site keeps
resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from . import check_jobs
from .errors import Denied
from .fs import ID, now, text_preview, write_json


class ContextCheckToolsMixin:
    def _op_check_job(self, args: dict) -> dict:
        action = args['action']
        has_script = args.get('script') is not None
        has_commands = args.get('commands') is not None
        has_batch = args.get('batch_name') is not None
        custom = has_script or has_commands or has_batch
        if sum((has_script, has_commands, has_batch)) > 1:
            raise Denied('script, commands and batch_name are exclusive')
        if action != 'start' and (custom or 'scratch_paths' in args or 'batch_name' in args):
            raise Denied('Custom check input requires action=start')
        if action == 'start' and args.get('scratch_paths') and not custom:
            raise Denied('scratch_paths requires a custom experiment script, commands or batch_name')
        if action == 'start':
            commands = args.get('commands')
            if has_batch:
                raw = args.get('batch_name')
                if not isinstance(raw, str) or ID.fullmatch(raw) is None:
                    raise Denied('Unknown check batch')
                batches = getattr(self.project, 'recorded_batches', {})
                if not isinstance(batches, dict) or raw not in batches:
                    raise Denied('Unknown check batch')
                commands = list(batches[raw])
            return check_jobs.submit(self, args.get('script'), commands=commands,
                                     fresh=args.get('fresh', False),
                                     purpose=args.get('purpose'), scratch_paths=args.get('scratch_paths'))
        if action == 'list':
            from .acceptance import discover, producer_diagnostics
            discovery = discover(self.project)
            return {'jobs': check_jobs.list_jobs(self.project, self.role.name, discovery=discovery),
                    'available': check_jobs.available(self, discovery=discovery),
                    'batch_names': sorted(self.project.recorded_batches),
                    **producer_diagnostics(discovery[1])}
        if not args.get('id'):
            raise Denied('Check result requires id')
        if action == 'join':
            return check_jobs.join(self, args['id'])
        if action == 'logs':
            return check_jobs.logs(self, args['id'], args.get('evidence_id'), offset=args.get('offset', 0), limit=args.get('limit', 8192), content_id=args.get('content_id'))
        if action == 'cancel':
            return check_jobs.cancel(self, args['id'])
        return check_jobs.result(self, args['id'], wait_seconds=args.get('wait_seconds', 0))

    def _op_verify(self, args: dict) -> dict:
        if "check_job" in self.role.capabilities:
            return check_jobs.submit(self)
        if not self.project.verify:
            raise Denied("No operator acceptance commands are configured")
        from . import acceptance
        bound = acceptance.scope(self.project, producer=True)
        if bound is not None and check_jobs.execution_key(self.config) != bound['execution']:
            bound = None  # A context cannot attest another execution config.
        self.close_exec()
        before = self.project.snapshots.capture_files(self.workspace)
        results = [self.sandbox.execute(self.workspace, command, writable=True)
                   for command in self.project.verify]
        after = self.project.snapshots.capture_files(self.workspace)
        passed = (all(r["exit_code"] == 0 and r["reason"] == "exited" for r in results)
                  and [r["script"] for r in results] == list(self.project.verify))
        representable = not before["skipped"] and not after["skipped"]
        unchanged = before["code_digest"] == after["code_digest"]
        dependencies_unchanged = bound is None or acceptance.scope(self.project, producer=True) == bound
        self.verification = {"passed": passed and unchanged and representable and dependencies_unchanged,
                             "dependencies_unchanged": dependencies_unchanged,
                             "snapshot_representable": representable,
                             "code_digest": after["code_digest"], "image": self.config.sandbox.image,
                             "unchanged_during_verification": unchanged,
                             "commands": [{"id": r["id"], "exit_code": r["exit_code"], "reason": r["reason"],
                                           "script": r["script"]} for r in results],
                             "run": self.run_dir.name, "created_at": now()}
        if bound is not None and 'cache_input_empty' in bound:
            self.verification.update({k: bound[k] for k in ('cache_input', 'cache_input_empty')})
        producer_record = dict(self.verification)
        if self.verification['passed'] and bound is not None and not self.tools_cancelled():
            producer_record['acceptance_scope'] = bound
        write_json(self.run_dir / "verification.json", producer_record)
        if producer_record.get('acceptance_scope') is not None:
            acceptance.recover_run(self.project, self.run_dir.name)
        return {**self.verification, 'commands':[
            {**command, 'script':text_preview(command['script'],512)[0],
             'script_truncated':text_preview(command['script'],512)[1]}
            for command in self.verification['commands']], 'evidence':'verification.json'}

