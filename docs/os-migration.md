# Migration to the kernel

This document is the plan for moving Mizu from a harness with a built-in
development workflow to the kernel specified in [kernel.md](kernel.md). It is
the end state the project works toward; slices are steps toward it, and a
stage is complete only when its dedicated code is deleted.

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

Today's behaviour does not disappear: it becomes a configuration. The project
ships it as `examples/development/`, an example userland (kind declarations,
roles, subscriptions, outcomes and policy files) that reproduces the current
workflow on the kernel. A deployment uses it by including it in its own
configuration and changes it freely. The kernel never reads it specially.

## Stages

Each stage: implement the kernel part, express the affected workflow in the
example userland, switch the code paths to the kernel part, delete the
dedicated code, keep CI green. Measure host CPU of an idle deployment and of
one change before and after the stages that touch the daemon (4 onward).

| Stage | Kernel work | Moves to userland | Deleted when done |
|---|---|---|---|
| 1. Objects and events | `mizu.kernel` package: kind declarations, object store with revisions and CAS, requirements, event log, subscriptions and cursors, wake files; `objects_*` and `events_*` tools; `[kinds]` and role `subscribe` configuration | — | — (foundation) |
| 2. Proposals, decisions, questions | Nothing new | Insight, decision, question, answer and notice kinds and their transitions; who is addressed; urgency (or none) | insights, insight_index, insight_tools, decision_delivery, decision_reconciliation, decision_idle, record_migration, operator_records, operator_record_index, host_records and the insight branches in shared modules |
| 3. Work items, waits, completion | Requirements over evidence objects; instant fields | Work-item kind, its states and transitions, wait kinds as fields with instants or refs, completion requirements (a check object bound to the snapshot) | work_items and work_item_* modules, completion_evidence and completion_* modules, ready-work projection |
| 4. Wake and scheduling | Daemon sleeps on events and the earliest instant; outcome effects from configuration | Outcome names and effects per role; what wakes each role | idle_seconds re-evaluation loop, readiness recheck cadence, event-recovery polls, CI re-wake cadence, fixed wake order in runtime/scheduling, progress outcome lists |
| 5. Drivers | Driver change notifications as events; generic driver operations | Version control, CI, pull requests, publication and review as driver operations plus kinds and requirements (for example a publication transition requiring an approval object) | ci_* modules, the pull-request model in vcs, reviews, publication-state projection, `"main"` literals, `GO <branch>` title parsing, CI wait kinds |
| 6. Child units and the writer | Child units started asynchronously, their result delivered as an event; workspace write is a resource with one holder | Delegation and consultation as role configuration; work order, continuation and parking rules as policy | delegation/consult special cases, work_order, continuations, `workspace == "write"` behavioural branches |
| 7. Text and periphery | Unit openings and tool descriptions state facts only | Behavioural guidance in role policy files | instructional prompt text, installer source testing, smoke retention and `smoke_refs` in record schemas |

The first stage-2 cutover is a shadow, not a change of authority. A deployment that includes `examples/development/kinds.toml` gets a `KernelShadow` wired into `Insights`: new submissions, content revisions and substantive accept/modify/reject decisions are mirrored into kernel objects with deterministic IDs taken from the legacy identity/key. Deferrals and withdrawals stay legacy-only until their instant and principal mappings are proven. The legacy files remain authoritative, no legacy read consults the kernel, and a derived projection failure never changes, blocks or replays a legacy result.

## Invariants kept throughout

Authentication of actors, append-only history, compare-and-swap, fail-closed
validation, bounded reads, sandbox isolation, grants, budgets and the
single-holder property of a writable workspace are kernel integrity and stay.
The single-holder property is a resource property, not a role type.

## Done

- An idle deployment's daemons use near-zero CPU regardless of how many
  objects and events are retained; they wake only on subscribed events and
  declared instants (plus one bounded driver observation where the external
  system cannot notify the host).
- Adding a kind, a transition, an event or a role, or removing review or
  audit, needs configuration only.
- `src/mizu` contains no role type, record state list, decision vocabulary,
  forge or CI concept, or behavioural prompt text.
- A deployment using `examples/development/` behaves as today.
