"""Context tool orchestration for sandbox execution and experiments."""
from __future__ import annotations

from . import check_jobs
from .errors import Denied
from .fs import text_preview, write_json


def exec_experiment(context, op: str, args: dict) -> dict:
    if "check_job" in context.role.capabilities and "source_receipt" not in args and (op == "experiment" or context.role.workspace == "read"):
        return check_jobs.submit(context, args["script"], kind=op)
    if context.role.workspace == "none":
        raise Denied("This role has no workspace")
    source = {"source_receipt": args["source_receipt"]} if "source_receipt" in args else {}
    result = context.sandbox.execute(context.workspace, args["script"],
                                     writable=context.role.workspace == "write", experiment=op == "experiment", **source)
    if op == "experiment":
        science = {key: args.get(key, "") for key in ("question", "comparison", "measure")}
        write_json(context.run_dir / "experiments" / f"{result['id']}.json", {**science, "command": result})
    # The command record is complete on disk. Tool results have a fixed
    # small shape before execution; escaped output cannot invalidate success.
    response = {key:result[key] for key in ('id','kind','exit_code','reason','seconds','writable') if key in result}
    for key in ('script','stdout','stderr'):
        response[key],response[key+'_truncated'] = text_preview(result.get(key,''))
    response['run'] = context.run_dir.name
    response['evidence'] = ('experiments/' if op == 'experiment' else 'commands/')+result['id']+'.json'
    return response
