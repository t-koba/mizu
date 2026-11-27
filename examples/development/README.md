# Development userland (example)

This directory expresses communication for a development deployment as queue
declarations and role policy text on the kernel
([docs/kernel.md](../../docs/kernel.md)). It is an example: a deployment
includes it from its own configuration and changes it freely. The kernel reads
nothing here specially.

- `queues.toml` declares four queues: `proposals` (knowledge and ideas for the
  product), `questions` (threads: what only the operator can settle, and the
  operator's answers referring to it), `notices` (operator and host facts for
  the roles) and `work` (stage-3 work messages: items,
  transitions, waits and completions).
- `policies/messages.md` says what a message in each queue means and what its
  reader does with it. Compose it into the role policies of the roles that
  read or write these queues.
- `convert_work.py` is an operator-run, userland-only procedure with two
  mutually exclusive modes. Inventory mode (`PROJECT [--limit N] [--closed N]`)
  copies a bounded work-item inventory into `work` messages with deterministic
  ids; its fields are previews, so that result is a projection, not a lossless
  conversion. Chain mode (`--project-root DIR --item wi_<32hex>`) replays one
  stored item's creation record, numbered revisions and optional retirement
  sidecar into declared `item`, `transition`, `wait` and `completion` messages,
  carrying full text with no previews or skipped declared fields, including the
  item's provenance as a canonical-JSON `origin`. Both modes use caller ids
  chosen for idempotent retries, change no runtime authority and
  are not product commands. `--dry-run` prints planned messages without
  appending; mixing the two modes' options fails closed.
- `work_view.py` is the operator-run proof reader for those messages:
  `python3 examples/development/work_view.py PROJECT [--pages N]` (N is 1-64,
  default 64). It pages the `work` queue through the public CLI with explicit
  cursors of 512 rows, validates each kernel row and declared body, and replays
  them into deterministic JSON sorted by item id. Explicit cursors include rows
  already delivered to the host: the reads record delivery facts but do not
  change work-item authority. A final page still reporting `more` is refused.
- `verify_work_conversion.py` is the operator-run pre-cutover gate:
  `python3 examples/development/verify_work_conversion.py --project-root DIR [--pages N]`
  (N is 1-64, default 64). It reads a complete work-items inventory (100 open
  and 100 closed rows), replays each stored chain into the same declared
  current view as the queue, and compares the two as exact canonical JSON
  restricted to the inventoried ids. An incomplete page, duplicate or
  malformed id, missing chain, malformed row, extra queue item or any
  mismatch is a bounded refusal; success prints only
  `{"project": name, "checked": N, "matched": true}`. It writes no work items
  and switches no authority.
- `outcomes.toml` declares which finish outcomes have each positive progress
  effect (daemon self-admission, no-progress policy, writer continuation,
  idle diagnostic and wait-style ready-work acceptance). It is stage-4
  userland: the runtime reads these names from configuration, not source.
- `observation_queues.toml` declares `vcs_observations`, `ci_observations`,
  `ci_head_state_observations` and `ci_summary_observations`: bounded queues to
  which observation drivers
  append what they find. Their bodies are opaque plain objects; the schemas
  enforce bounds and required fields only. It is stage-5 userland. A config
  written by `mizu configure` already declares the three CI queues, so including
  this fragment there would duplicate those keys.
- `observation_drivers.toml` declares the illustrative `ci_costs`,
  `ci_head_state` and `ci_summary` drivers in `drivers/`. Including them switches
  the `vcs_read ci_costs` join, the fixed `ci_head_state` rows and the opt-in
  `ci_summary` aggregates to those operator-owned programs through the generic
  host seam; without them the op fails closed and those prompt facts are empty.
  A config written by `mizu configure`
  already carries working declarations with absolute, CWD-independent commands
  pointing at durable copies of the programs in the config dir, so including
  this fragment there would duplicate the keys. A manually authored or migrated
  config must copy each reviewed program into a durable location that survives
  upgrades (the config dir) and declare an absolute argv of its Python
  interpreter and that copy, as `mizu configure` generates; the relative argv
  here is illustrative only, not a usable deployment command.
- `policies/observations.md` says what an observation in each queue means, who
  reads it and what the reader does. Compose it into the role policies of the
  roles that read those queues.

`work` is only the stage-3 message form. The dedicated work-item store stays
authoritative until cutover; declaring the queue changes no runtime authority.

Declaring the observation queues changes no runtime authority on its own. The
separate driver declarations do switch the CI cost computation, the fixed
head-state rows and the opt-in aggregate prompt facts to operator-owned
programs; their dedicated kernel modules are gone. The remaining dedicated version-control and CI code stays authoritative
until later cutover and deletion slices. Only the example driver commands are
shipped here; a deployment supplies its own.

Roles reach the queues with the `queues` capability and the `queue_*` and
`message_*` tools. The operator and host tooling use
`mizu queue PROJECT write|read|ack|message|refs` (see
[docs/operations.md](../../docs/operations.md)). Which queues wake which
roles is the `wake` list of each declaration; a reply also wakes the author of
the message it refers to.

To use it in a manually authored config, include the files and add the policy
part. For a config created by `mizu configure`, omit the two observation
fragments: its CI observation queues and drivers are already declared and
including them again is a duplicate-key error.

```toml
include = ["../examples/development/queues.toml", "../examples/development/outcomes.toml",
          "../examples/development/observation_queues.toml",
          "../examples/development/observation_drivers.toml"]

[roles.worker]
policy = ["policies/shared.md", "policies/worker.md",
             "../examples/development/policies/messages.md",
             "../examples/development/policies/observations.md"]
capabilities = [..., "queues"]
```

The declarations name `role:worker` and `role:auditor`; rename them to the
roles of the deployment.
