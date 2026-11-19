"""Generic model-facing tools over the project's kernel stores.

Trust: capability and argument validation run before any object or event I/O,
including on the direct dispatch path. The acting role is taken from the
trusted Context: no tool argument can select a principal or another role's
event cursor. Kinds, fields, states, transitions and subscriptions come only
from configuration; this module names none of them.
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


def objects_create(context, args: dict) -> dict:
    _gate(context, "objects_create", args)
    return context.project.kernel_objects.create(args["kind"], args["fields"], _actor(context),
                                                 args.get("id"))


def objects_transition(context, args: dict) -> dict:
    _gate(context, "objects_transition", args)
    return context.project.kernel_objects.transition(
        args["kind"], args["id"], args["transition"],
        expected_rev=args["expected_rev"], fields=args.get("fields") or {},
        actor=_actor(context), snapshot=context.snapshot.get("id"))


def objects_read(context, args: dict) -> dict:
    _gate(context, "objects_read", args)
    return context.project.kernel_objects.read(args["kind"], args["id"], args.get("rev"))


def objects_list(context, args: dict) -> dict:
    _gate(context, "objects_list", args)
    options = {"state": args.get("state"), "where": args.get("where"),
               "after": args.get("cursor")}
    if "limit" in args:
        options["limit"] = args["limit"]
    return context.project.kernel_objects.list(args["kind"], **options)


def events_read(context, args: dict) -> dict:
    _gate(context, "events_read", args)
    options = {"after": args.get("cursor")}
    if "limit" in args:
        options["limit"] = args["limit"]
    return context.project.kernel_events.pending(context.role.name, **options)


def events_ack(context, args: dict) -> dict:
    _gate(context, "events_ack", args)
    return {"seq": context.project.kernel_events.ack(context.role.name, args["seq"])}
