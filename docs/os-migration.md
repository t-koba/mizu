# Migration to the kernel

This document records the plan for moving Mizu from a harness with a built-in
development workflow to the kernel specified in [kernel.md](kernel.md). Stage 2
is complete: the proposal, decision, question, answer, notice and receipt
machinery was deleted from `src/mizu`, and those flows are declared queues and
policy text. A stage is complete only when its dedicated code is deleted.

## Why

An inventory of `src/mizu` at 1322c64f (127 modules, 57,460 lines) found that
about 47% of the code exists to implement one development workflow:
proposals and decisions, work items and their waits, completion acceptance,
publication and review, CI handling, delegation and consultation, and
self-hosting. Nine shared modules (runtime, config, protocol, unit_finish,
unit_prompts, project, cli, storage, snapshot; about 13.5k lines) branch on
the same concepts. Every new need of a deployment therefore becomes a product
change, and the product drifts toward one deployment's process.

## Userland

Today's behaviour does not disappear: it becomes configuration and policy
text. The project ships it as `examples/development/`, an example userland
(queue declarations, roles, wakes, outcomes and policy files) that reproduces
the current workflow on the kernel. A deployment uses it by including it in its
own configuration and changes it freely. The kernel never reads it specially.

What a message means, and what a role does on reading it, is written in the
role policy files. For example, "a proposal is decided by the writer, who
writes a decision message referring to it" is a sentence in the writer's
policy and a `decisions` queue in the configuration, not code.

Existing stored records are converted by operator-run procedures that append
equivalent messages to the declared queues; they are not part of the kernel or
of a running role. The example userland declares the `proposals`, `questions`
(threads: questions and the answers that refer to them) and `notices` queues
with their schemas, roles and wakes. There is no
authority marker, dual-write path, or adapter left in the runtime: decisions are
plain-English judgements made by roles under policy, and acknowledgements are
queue cursors. Old record files appear only as stored data for those conversions
and as historical manifests; see [operations](operations.md) for the procedures.

## Stages

Each stage: implement the kernel part, express the affected workflow in the
example userland, switch the code paths to queues, delete the dedicated code,
keep CI green. There is no shadow or dual-write period in product code: a stage
switches authority at once together with the one-time conversion, then deletes
the dedicated code. Measure host CPU of an idle deployment and of one change
before and after the stages that touch the daemon (4 onward). The repeatable local harness is
`python3 scripts/daemon-cpu.py --report private-validation/daemon-cpu.json`; it
runs real daemon subprocesses (Unix-only), waits for an observed steady-state CPU
window, and records measurement validity in the receipt. It also records the
deployment shape, so only the relative before/after comparison on one machine is
meaningful; a non-zero exit means that window was not steady state.

| Stage | Kernel work | Moves to userland | Deleted when done |
|---|---|---|---|
| 1. Queues | `mizu.kernel`: queue declarations, authenticated append, per-reader cursors and acknowledgement, delayed delivery, reference lookup, retention, wake files; `queue_*` and `message_*` tools; `[queues]` configuration | — | The object store with states, transitions and requirements (`kernel/kinds.py`, `kernel/objects.py`, `kernel/events.py` as they are), `kernel_shadow.py`, `kernel_shadow_audit.py`, the `kernel-shadow` commands, `[kinds]` and role `subscribe` configuration, `objects_*` and `events_*` tools |
| 2. Proposals, decisions, questions, answers, notices | Nothing new | Queues for each, and the policy text for who writes and reads them and what a reader does | insights, insight_index, insight_tools, decision_delivery, decision_reconciliation, decision_idle, record_migration, operator_records, operator_record_index, host_records and the insight branches in shared modules |
| 3. Work items, waits, completion | Nothing new | Work as messages in a queue, waits as delayed messages or as reading for a referring message, completion as policy (the holder's judgement and an independent check) | work_items and work_item_* modules, completion_evidence and completion_* modules, ready-work projection |
| 4. Wake and scheduling | Daemon sleeps on wakes and the earliest `deliver_at`; outcome effects from configuration | Outcome names and effects per role; which queues wake each role | idle_seconds re-evaluation loop, readiness recheck cadence, event-recovery polls, CI re-wake cadence, fixed wake order in runtime/scheduling, progress outcome lists |
| 5. Drivers | Driver observations appended to configured queues; generic driver operations | Version control, CI, pull requests, publication and review as driver operations plus queues and policy | ci_* modules, the pull-request model in vcs, reviews, publication-state projection, `"main"` literals, `GO <branch>` title parsing, CI wait kinds |
| 6. Child units and the writer | Child units started asynchronously, their result appended to a queue; workspace write is a resource with one holder | Delegation and consultation as role configuration; work order, continuation and parking rules as policy | delegation/consult special cases, work_order, continuations, `workspace == "write"` behavioural branches |
| 7. Text and periphery | Unit openings and tool descriptions state facts only | Behavioural guidance in role policy files | instructional prompt text, installer source testing, smoke retention and `smoke_refs` in record schemas |

## Invariants kept throughout

Authentication of authors, append-only history, fail-closed validation,
bounded reads, sandbox isolation, grants, budgets and the single-holder
property of a writable workspace are kernel integrity and stay. States, waits
and completion are userland policy over queue messages and work-item records;
the kernel enforces only integrity and bounds.

## Done

- An idle deployment's daemons use near-zero CPU regardless of how many
  messages are retained; they wake only on messages in queues that wake them
  and on due delayed messages (plus one bounded driver observation where the
  external system cannot notify the host).
- Adding a queue or a role, or removing review or audit, needs configuration
  and policy text only.
- `src/mizu` contains no role type, record state or lifecycle, decision or
  answer vocabulary, forge or CI concept, or behavioural prompt text.
- A deployment using `examples/development/` behaves as today.
