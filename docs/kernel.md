# Kernel

Mizu is an operating system for autonomous model roles. This document is the
normative specification of what the kernel provides. Everything not described
here is deployment policy: it lives in the deployment's configuration and
policy files, and the kernel neither knows nor enforces it.

## What the kernel is and is not

The kernel provides seven kinds of mechanism:

| Part | Provides | Never decides |
|---|---|---|
| Units | Start, stop, interrupt and resume a unit of model work for a configured role; engine drivers, sessions, resource bounds, sandboxed execution, isolation, tool grants | Which role exists, what it should do, what a good result is |
| Scheduler | Execution slots, the admission queue, cache-aware placement; wakes a role only on an event it subscribes to or at a known instant | Priorities between kinds of work, what counts as progress |
| Objects | Typed, revisioned records whose kinds, fields, states, transitions, permissions and transition requirements are declared by configuration; content-addressed snapshots; command and check evidence | Which kinds exist, what their states mean, what completion requires |
| Events | Every object creation and transition, every check completion and every driver notification is an ordered, named event; roles subscribe by name | Which events matter to whom |
| Accounting | Request budgets, token usage, time | Spending policy |
| Drivers | Opaque operations on external systems (version control, CI, search, dependency materialisation) and their change notifications | Forge, CI, branch or review semantics |
| Tools | Model-facing operations over the parts above | How a model should behave |

Rules for kernel code:

- No role, purpose, record kind, state name, event name, branch name or
  workflow appears in kernel code. Documented defaults for mechanism
  parameters are fine; example configurations may ship under `examples/`.
- The kernel enforces integrity only: authentication of the actor,
  compare-and-swap on revisions, append-only history, fail-closed validation
  of declared schemas, bounded reads and writes, isolation and grants.
- Model-facing text states facts: which tools exist, what each does, what an
  outcome causes. Instructions about behaviour belong to role policy files.
- The kernel does no work on a fixed cadence. A daemon sleeps until an event
  it subscribes to is appended or the earliest pending instant is reached.
  External systems that cannot notify the host are observed by a driver, which
  turns observed changes into events; the cost of an unchanged observation is
  one bounded driver call.

## Objects

An object is one record of a declared kind.

```json
{"kind": "<kind>", "id": "<id>", "rev": 3, "state": "<state>",
 "fields": {...}, "author": "<principal>", "created_at": "...",
 "updated_at": "...", "transition": "<name of the transition that made rev>",
 "actor": "<principal>"}
```

- `id` is kernel-assigned (32 lowercase hex). `rev` starts at 1 and increases
  by one per transition or field update. Every revision is stored create-only
  under `objects/<kind>/<id>/<rev>.json`; the highest revision is current.
- Writes take the project record lock, check `expected_rev` (compare-and-swap)
  and validate the new revision fully before publishing it. A failed write
  publishes nothing.
- Principals: `role:<name>` (an authenticated unit of that role), `host`
  (operator CLIs and drivers acting through the host), `operator`
  (an operator-authenticated CLI call). `author` is the creating principal;
  `holder` is an optional field-level principal declared by the kind.

### Kind declarations

```toml
[kinds.note]
states = ["open", "closed"]
initial = "open"
create = ["role:writer", "operator"]       # principals allowed to create
fields.title = {type = "string", max = 200, required = true}
fields.body = {type = "string", max = 65536}
fields.evidence = {type = "ref", kinds = ["check"]}
fields.due = {type = "instant", event = "note.due"}

[[kinds.note.transitions]]
name = "close"
from = ["open"]
to = "closed"
by = ["author", "role:reviewer"]
requires = [{ref = "evidence", state = ["passed"]}]
fields = ["body"]                           # fields the transition may set
emits = "note.closed"                       # default: "<kind>.<name>"
```

- `states` is a non-empty list of names; `initial` is one of them.
- Field types: `string` (with `max`), `integer` (with `min`/`max`),
  `boolean`, `instant` (aware ISO time), `ref` (an object id, optionally
  restricted to `kinds`), `list` of one of these (with `max_items`), and
  `principal`. `required` fields must be present at creation.
- A transition names the states it leaves (`from`, or `["*"]`), the state it
  enters (`to`), the principals that may make it (`by`; `author` and
  `holder` resolve against the object), the fields it may set, its
  requirements, and the event it emits.
- A transition `to` the same state is an update. Without a declared
  transition no field can change after creation.
- Unknown keys, unknown types, undeclared states and unreachable `initial`
  states refuse the configuration.

### Requirements

A requirement is a predicate the kernel evaluates when a transition is
requested. The transition is refused, naming the failed requirement, unless
every requirement holds.

```toml
requires = [
  {ref = "evidence", state = ["passed"], where = {snapshot = "$snapshot"}},
  {field = "summary", present = true},
]
```

- `ref`: the field holds an object id; that object must exist, be of an
  allowed kind, be in one of `state`, and match `where`.
- `where` compares fields of the referenced object with literals,
  `$field.<name>` (a field of the transitioning object) or `$snapshot` (the
  actor's current published snapshot id).
- `field ... present`: the field is set after the transition.

The kernel has no other built-in predicates. Evidence that a check passed is
an object of a kind the deployment declares and the check runner writes.

## Events

- Each object creation emits `<kind>.created` (or the kind's declared
  `created_event`), and each transition emits its declared event. The event is
  appended to the project's event log in the same locked operation that
  publishes the revision.
- Event record: `{"seq": n, "name": "...", "kind": "...", "id": "...",
  "rev": r, "from": "...", "to": "...", "actor": "...", "at": "..."}`.
  `seq` is a gap-free per-project sequence. The log is append-only and stored
  create-only under `events/<seq>.json`.
- Host components emit events through the same append operation:
  check completion (`check.<status>` style names are policy chosen by the
  check kind), driver notifications (the driver names its events), instants
  reaching their time (the field's declared `event`).
- Event names are lowercase dotted names. The kernel attaches no meaning to
  any name.

### Subscriptions and delivery

```toml
[roles.reviewer]
subscribe = ["note.closed", "check.*"]
```

- A role subscribes to event names; `*` matches one dotted component, `**`
  matches any non-empty suffix and is valid only as the final component.
  A non-terminal `**` is refused and never matches.
- Each role has a durable cursor (the last acknowledged `seq`). A unit reads
  the events after its cursor that match its subscriptions, bounded per page,
  and acknowledges explicitly. Unacknowledged events are delivered again.
- Appending an event that matches a role's subscription marks the role as
  woken: the kernel touches `wake/<role>`, which the service manager watches
  (systemd PathChanged, launchd WatchPaths, or the resident daemon's own
  watch). No role is woken by a timer except at a declared instant.

## Instants

A field of type `instant` with an `event` name schedules that event for that
instant. The scheduler keeps the earliest pending instant and sleeps until it,
or until the next event, whichever comes first. When the instant passes the
kernel appends the event once and then treats the field as consumed for that
value. The firing reserves its sequence and full event (including the timestamp)
before appending it; if the process stops or clearing the timer fails, recovery
repeats that exact event without duplicating it and then clears the timer.

## Finishing a unit

A unit ends with `finish`. The outcomes a role may use and their effects are
declared per role:

```toml
[roles.writer.outcomes]
continue = {readmit = true}
wait = {readmit = false}
done = {readmit = false}
```

- `readmit = true` asks the scheduler to admit the role again as soon as a
  slot is free. Otherwise the role sleeps until an event or instant wakes it.
- The kernel attaches no other meaning to an outcome name. Requirements on
  finishing (for example "a check object must have passed") are expressed as
  requirements on the transitions the role makes, not on the outcome.

## Tools

The model-facing object and event tools are generic:

| Tool | Effect |
|---|---|
| `objects_create(kind, fields)` | Create an object of a declared kind if the caller may create it |
| `objects_transition(kind, id, transition, expected_rev, fields)` | Make a declared transition; refused with the failed requirement named |
| `objects_read(kind, id, rev?)` | Read the current or a given revision |
| `objects_list(kind, state?, where?, cursor?, limit?)` | List current objects, bounded and paged |
| `events_read(cursor?, limit?)` | Read subscribed events after the role's cursor, bounded |
| `events_ack(seq)` | Advance the role's cursor |

Grants: `objects` and `events` capabilities in the role's capability list;
per-kind and per-transition principals in the kind declaration. No role holds
either grant by default. The host binds the acting principal as `role:<name>`
and binds every event read and acknowledgement to that role; tools carry no
actor or role argument.

Object ids are kernel-assigned 32-hex strings. `objects_list` orders by id;
`cursor` is the last id of the previous page (`next`, exclusive), `state`
filters by declared state name and `where` matches exact field values.
`events_read` starts after the role's durable cursor, or after `cursor` (a
previous page's `next`) when supplied, and returns `next`, `head` and `more`.
Both pages default to 64 rows and are capped at 512; `objects_read` and
`events_ack` refuse out-of-range revisions and sequences.

## Drivers

A driver is an operator-configured command speaking a JSON request/response
protocol. The kernel passes requests and returns responses without
interpreting provider concepts. A driver that observes an external system
reports changes as events (`events` in its response); the kernel appends them
under the driver's configured event prefix. Version control, CI, pull
requests, publication and review are driver operations plus deployment
policy; none of them is a kernel concept.

## Bounds

Bounds are either fixed mechanism limits or configuration with documented
defaults: object and field sizes, page sizes, event page sizes, log retention.
Reads are bounded per response, never in total reachability. Current mechanism
defaults are a 1 MiB + 4 KiB object record, a 64 KiB event and 100,000,000
revisions per object; tool pages default to 64 rows and are capped at 512.
A tool field map holds at most 64 properties; declared field types and their
own bounds are authoritative.
