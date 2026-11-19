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

The first stage-2 cutover is a shadow, not a change of authority. A deployment that includes `examples/development/kinds.toml` gets a `KernelShadow` wired into `Insights`: new submissions, content revisions and all recorded decisions are mirrored into kernel objects with deterministic IDs: exact 32-hex legacy identities are preserved, other legal legacy IDs and decisions are hashed into the kernel ID grammar. A deferral also projects its state transition and an aware ISO revisit instant; a terminal transition retires that instant, while an unparseable revisit still projects the decision but leaves the kernel state unchanged and is not reported as divergence; a later revision re-addresses the projected holder, and reprojection repairs diverging fields through the declared revise transition. Decision objects are created before insight state transitions so a rejected decision field cannot leave a half-written projection. The legacy files remain authoritative, no legacy read consults the kernel, and a derived projection failure never changes, blocks or replays a legacy result. `mizu kernel-shadow PROJECT INSIGHT` reports one fact pair against its objects; `--reproject` is an explicit repair, not a read.

Operator notice writes are mirrored under the same boundary: for a deployment that declares the `notice` kind, each accepted `operator.notice` record creates one create-only kernel object whose ID is derived deterministically from the legacy record ID. Its fields carry the computed record digest, the envelope kind, the notice body and, when it fits the declared reference bound, the closure target or subject ID. An exact retry leaves the object unchanged; an existing object with different fields raises a diagnostic denial. The kernel contributes no routing or acceptance decision: legacy files remain authoritative, no legacy read consults the kernel, and a failed notice projection is only a bounded failure record, never a changed, blocked or replayed legacy write.

Questions and answers stay behind the same boundary. For a deployment that declares the `question` and `answer` kinds, an insight submitted to the operator and a role record with the `operator_question` route each create one create-only kernel question object with a deterministic ID hashed from its authoritative identity (type, insight ID and revision, or type, record ID and computed digest); a revised insight question creates the object for its new revision. Every accepted or no-op insight submit and revise, including exact and retained retries, replays the question projection with the current authoritative record, so a failed mirror is repaired by the next accepted write. The declared question and insight body bounds match the authoritative legacy insight body bound, so a legal long body is representable. Its fields carry the declared subject shape, body and optional urgency; a role record without a representable body fails only the projection. Each accepted answer record creates a create-only answer object referencing that question, carrying the revision, action, body and the legacy record ID when it fits the declared reference bound. The answer object is written before the question transition: only `publish` moves a pending question to `answered` as `host`, other actions leave its state unchanged, and an exact retry leaves both objects unchanged. Question and answer projections share the replay boundary: `mizu kernel-shadow PROJECT INSIGHT --reproject` also replays the insight question and every answer record whose subject is that exact insight, scanning operator records under the configured catalog bound, and the report adds observational `question_verification` and `answer_verification` results; a plain command verifies without writing. No legacy read consults kernel state. An answer to a missing question, a reused projection identity with different content, or any other write-site failure is a bounded diagnostic record, never a changed, blocked or replayed authoritative write.

The read-only `KernelShadow.verify_notice`, `verify_question` and `verify_answer` helpers compare accepted envelopes with their declared objects and report bounded field, object and expected-state mismatches without writing or changing legacy authority.

`mizu kernel-shadow-audit PROJECT` is the bounded stage-2 proof: a read-only pass over the live insight page (capped by `INSIGHT_RETRIEVAL_LIMIT`), operator records and declared `operator_question` role records (each capped by `[limits].operator_record_catalog_scan_entries`) verifies every declared insight, decision, notice, question and answer projection against the authoritative legacy files, reporting exact counts, bounded mismatches and truncation flags. It never writes kernel objects or legacy records, never reprojects, and never changes legacy authority; a clean report is completion evidence for the shadow only, not an authority cutover or permission to delete the dedicated stage-2 modules.

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
