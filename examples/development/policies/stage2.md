# Stage-2 role policy (example)

This file is deployment policy, not kernel code. It gives the meanings that
`queues.toml` declares for stage 2: proposals and decisions, questions and
answers, notices, and converted decision receipts. The kernel enforces the declared grants and schemas; it
does not know these names, infer states from them, or give a message authority
over another message.

## Proposals and decisions

- Any role, the operator and the host may append to `proposals`. The writer
  reads that queue and wakes on it. A role that submits a finding writes a
  bounded `title` and `body`; it does not decide its own finding.
- The writer decides by appending to `decisions` with `refs` to the proposal
  being decided. The declared action and reason are copied from the writer's
  judgement; the kernel only checks the schema and the granted principal.
- A `defer` carries the exact legacy revisit condition in the declared
  `revisit` field. When that condition names an aware instant, the writer also
  appends a delayed message to `decisions`, with the same action, reason and
  revisit and a reference to the same proposal. The delayed message becomes
  readable at its instant; until then it blocks later messages for readers of
  that queue. A free-text condition has no delayed message and remains in the
  decision body.
- A later decision referring to an earlier one is data for the deployment's
  readers. The kernel records the reference but attaches no withdrawal,
  supersession or state transition to it.

## Questions and answers

- A role appends to `questions` when it needs an operator decision or fact. The
  body carries the question, optional context and optional default; the author
  is bound by the kernel from the authenticated unit, never from a tool
  argument. The operator and roles read this queue.
- The operator answers by appending to `answers` with `refs` to the question.
  Readers join an answer to its question through that reference. A legacy
  answer to an insight revision instead references that revision's converted
  proposal, and refuses conversion when no proposal was converted for it. An
  answer is not a command and does not change any other record unless the
  deployment's own tooling says so.
- All roles and the operator may read answers. Acknowledgement is explicit; an
  unacknowledged answer remains in the role's delivery view.

## Notices

- The operator or host writes notices; all roles and the operator read them.
  A notice informs its readers. It grants no capability and changes no workflow
  state by itself.

## Converted receipts

The host writes converted legacy decision receipts to `receipts`; every role,
the operator and the host may read them. A message carries the legacy token,
role, inferred action and, for a corrupt receipt, its bounded reason. Receipts
are conversion facts, not kernel state: they do not grant, deny or acknowledge
anything by themselves. A queue-backed reader may suppress the matching
token's pending decision or attention and surface the legacy diagnostic.

## Boundaries

- `stage2_view.py` is a read-only operator projection for inspecting these
  meanings. It is not an authority and does not replace a role's own granted
  tools.
- `convert_legacy.py` is a one-time operator-run projection of legacy records
  into these queues. While it is used, the legacy stores remain authoritative
  and no runtime route changes.
- Adding a queue, changing a schema, or changing who reads or writes a message
  is a change to this example's configuration and policy text, not to the
  kernel.
