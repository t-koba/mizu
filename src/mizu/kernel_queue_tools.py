"""Generic model-facing tools over the project's declared message queues.

Trust: capability and argument validation run before any queue I/O, including
on the direct dispatch path. The acting author is bound to the trusted Context
role; no argument can select a principal, another role's cursor, or a queue
whose declaration denies that author. Queue names, message meanings and
references come only from deployment configuration; this module names none of
them.
"""
from __future__ import annotations

from .errors import Denied
from .protocol import DEFINITIONS, operation_allowed, validate


def _gate(context, operation: str, args: dict) -> None:
    if not operation_allowed(operation, context.role.capabilities):
        raise Denied(f"Role has no capability: {operation}")
    validate(args, DEFINITIONS[operation][1])


def _actor(context) -> str:
    return f"role:{context.role.name}"


def queue_write(context, args: dict) -> dict:
    _gate(context, "queue_write", args)
    options = {"refs": args.get("refs"), "deliver_at": args.get("deliver_at")}
    if "id" in args:
        options["id"] = args["id"]
    return context.project.kernel_queues.append(
        args["queue"], args["body"], _actor(context), **options)


def queue_read(context, args: dict) -> dict:
    _gate(context, "queue_read", args)
    options = {"cursor": args.get("cursor")}
    if "limit" in args:
        options["limit"] = args["limit"]
    return context.project.kernel_queues.read(args["queue"], _actor(context), **options)


def queue_ack(context, args: dict) -> dict:
    _gate(context, "queue_ack", args)
    return {"seq": context.project.kernel_queues.ack(
        args["queue"], _actor(context), args["seq"])}


def message_read(context, args: dict) -> dict:
    _gate(context, "message_read", args)
    return context.project.kernel_queues.message(args["id"], _actor(context))


def message_refs(context, args: dict) -> dict:
    _gate(context, "message_refs", args)
    options = {"cursor": args.get("cursor")}
    if "limit" in args:
        options["limit"] = args["limit"]
    return context.project.kernel_queues.refs(
        args["id"], args["direction"], _actor(context), **options)
