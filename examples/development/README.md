# Development userland (example)

This directory expresses a development workflow (proposals and their
decisions, questions and answers, notices, work and its completion, checks) as
queue declarations and role policy on the kernel
([docs/kernel.md](../../docs/kernel.md)). It is an example: a deployment
includes it from its own configuration and changes it freely. The kernel reads
nothing here specially. The stage-2 meanings are written in
[policies/stage2.md](policies/stage2.md).

`queues.toml` grows stage by stage as
[docs/os-migration.md](../../docs/os-migration.md) moves each workflow out of
the kernel code. A queue is added here in the same change that switches its
code path and deletes the dedicated module. `kinds.toml` belongs to the object
store that stage 1 replaces and is deleted with it.

## One-time legacy conversion

`convert_legacy.py` is an operator-run example-userland command. It reads the
current legacy insights and their effective decisions through the public
Insight API, declared role questions and their answers through the operator
record APIs, and operator notices from the same authenticated record store. It
appends equivalent messages to the `proposals`, `decisions`, `questions`,
`answers` and `notices` queues declared in `queues.toml`. Archived revisions
stay in the legacy store; nothing is rewritten and no runtime authority, route
or capability changes. Decision messages use the queue's declared writer
principal (`role:writer`) as historical authorship only. A deferral whose
revisit carries an aware instant also becomes a delayed decision message (same
action, reason and revisit, referencing the converted proposal) due at exactly
that instant; past instants are immediately due. Every converted defer decision
carries its exact legacy revisit condition in the declared `revisit` field,
whether or not it is an instant. A malformed instant refuses the run before any
append.

Converted questions derive their required `question` from the exact legacy
payload; the full payload, subject, authenticated writer, id and digest are
retained in declared optional fields. Answers carry the exact legacy body and
reference their converted question or, for an insight revision, that revision's
converted proposal; notices carry their exact body, action and optional closure
target. Legacy `operator.cli` authorship is retained in the body while the
message author is the queue's granted principal. An answer that does not bind an
exact declared role question or converted insight revision, a truncated
enumeration or a field that exceeds its declared bound refuses the whole run
before any append.

```sh
python3 examples/development/convert_legacy.py config/config.toml --project NAME [--apply]
```

Message ids are derived from legacy identity and content: insight id and
revision for proposals/decisions/revisits, record id and digest for
questions/answers/notices, each in its own domain, so a repeat appends nothing.
Both the dry run and `--apply` report `proposals`, `decisions`, `revisits`,
`questions`, `answers` and `notices` id lists.
Without `--apply` the command is a dry run: it prints the planned ids and writes
nothing. Malformed or ambiguous legacy input refuses the whole run before any
append.

## Stage-2 view

`stage2_view.py` is an operator-run, read-only projection of the queues above.
It calls `QueueStore.read` with the `operator` principal for `proposals`,
`decisions`, `questions`, `answers` and `notices`, then joins decisions and
revisits to proposals and answers to questions or converted proposals by message
refs. It prints JSON with each proposal's decision, answer and due delayed
revisits, each question's answer, the notice bodies, and every queue's `head`
and `delayed` flag. Every decision ref must resolve to a proposal id and every
answer ref to a proposal or question id; an unresolved ref refuses the view
instead of being silently ignored. Reads use an explicit cursor, bounded pages
and a page-count bound; the command never acknowledges or appends, so repeated
reads at the same instant agree. A message that is not yet due stops its
queue's read and is reported as `delayed` rather than skipped.

```sh
python3 examples/development/stage2_view.py config/config.toml --project NAME [--at 2099-01-01T00:00:00+00:00]
```

`--at` is an aware ISO instant used for delayed delivery; without it the kernel
uses the current time. The reader imports no legacy insight or operator-record
module and does not change runtime authority or routing.

## Conversion gate

`stage2_verify.py` is an operator-run, read-only migration gate. It recomputes
the current plan with `convert_legacy._plan` and compares it with the five
conversion queues on queue, id, author, body, ordered refs and delivery
instant. Reads use the `operator` principal, an explicit cursor, bounded pages
and a fixed far-future instant so delayed revisits are included; nothing is
appended or acknowledged. Missing, extra or changed messages, duplicate ids
and unreadable queues fail the gate.

```sh
python3 examples/development/stage2_verify.py config/config.toml --project NAME
```

It prints a JSON report (`project`, per-queue `checked` counts, `ok`,
`differences`) and exits 0 on equality, 2 on refusal or mismatch. It writes
nothing to legacy stores and changes no runtime authority.
