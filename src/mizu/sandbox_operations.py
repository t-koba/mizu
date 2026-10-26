"""Context tool orchestration for sandbox execution and experiments."""
from __future__ import annotations

from . import check_jobs
from .errors import Denied
from .fs import text_preview, write_json


#: Caller-selected execution mode for ordinary exec/experiment calls.
#: Omission and sync share the existing synchronous/direct path; only async
#: selects the queued path under the existing grants.
MODES = ("sync", "async")


def exec_experiment(context, op: str, args: dict) -> dict:
    mode = args.get("mode")
    if mode is not None and mode not in MODES:
        raise Denied("Unsupported execution mode")
    scratch_requested = bool(args.get("scratch_paths"))
    if scratch_requested and (op != "experiment" or mode != "async"
                              or "source_receipt" in args
                              or "check_job" not in context.role.capabilities):
        raise Denied("scratch_paths requires an async custom experiment without source_receipt")
    scratch = {"scratch_paths": args["scratch_paths"]} if scratch_requested else {}
    if mode == "async":
        if "check_job" not in context.role.capabilities:
            raise Denied("Async mode requires check_job")
        if "source_receipt" in args:
            raise Denied("Async mode cannot carry a source receipt")
        if not (op == "experiment" or context.role.workspace == "read"):
            raise Denied("Cannot detach live workspace writes")
        return check_jobs.submit(context, args["script"], kind=op, **scratch)
    # Omitted mode and sync share the direct path; only explicit async queues.
    if context.role.workspace == "none":
        raise Denied("This role has no workspace")
    source = {"source_receipt": args["source_receipt"]} if "source_receipt" in args else {}
    if op == "exec" and context.role.persistent_exec:
        result = context.persistent_execute(args["script"])
    else:
        result = context.sandbox.execute(context.workspace, args["script"],
                                         writable=context.role.workspace == "write", experiment=op == "experiment", **source)
    if op == "experiment":
        science = {key: args.get(key, "") for key in ("question", "comparison", "measure")}
        write_json(context.run_dir / "experiments" / f"{result['id']}.json", {**science, "command": result})
    # The command record is complete on disk. Tool results have a fixed
    # small shape before execution; escaped output cannot invalidate success.
    response = {key:result[key] for key in ('id','kind','exit_code','reason','seconds','writable','persistent_exec','state_lost','execution_may_be_partial','status_note') if key in result}
    for key in ('script','stdout','stderr'):
        response[key],response[key+'_truncated'] = text_preview(result.get(key,''))
    response['run'] = context.run_dir.name
    response['evidence'] = ('experiments/' if op == 'experiment' else 'commands/')+result['id']+'.json'
    return response
