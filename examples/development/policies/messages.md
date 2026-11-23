# Messages

Roles, the operator and host tooling communicate through the queues declared
in `queues.toml`. A unit opening states, for each queue you may read, how many
due messages you have not acknowledged and the newest of them. `queue_read`
returns them in order; `queue_ack` records that you have read up to a
sequence. Acknowledging says only that you read a message. It answers,
accepts or rejects nothing.

## proposals

A proposal is knowledge about the product: a finding, a measurement, an
outside fact such as a new upstream release, or the operator's view. It is
input to your own plan, not a request to you. Your plan is decided by what
makes the product better; a proposal you do not act on now still informs it.
When you create work because of a proposal, name the message id as the work
item's `origin`. A proposal needs no reply and has no state.

Write a proposal when you learned something about the product that the
worker does not know yet. Say what you observed and why it matters, with
enough detail to act on without asking you.

## questions and answers

Ask a question only when the operator alone can settle it. State the
question, the default you will follow until an answer arrives, and the
context needed to answer. Then act on your default; do not wait.

An answer is the operator's message in `questions` that refers to your
question; it arrives with the question it answers. When one arrives, it
replaces your default for that question from then on.

## notices

A notice is a fact from the operator or host tooling, for example that an
external service is down or that a release was installed. Take it into
account; it needs no reply.

## work

A work item is a message. A transition, a wait or a completion is a later message
referring to the earlier one it changes; a wait uses delayed delivery so it
wakes its readers when due. A transition or completion carries the evidence a
reader needs to judge the result. The kernel assigns no state or meaning to
these messages: it stores, delivers and references them; their meaning is this
policy.
