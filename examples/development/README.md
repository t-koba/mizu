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
- `outcomes.toml` declares which finish outcomes have each positive progress
  effect (daemon self-admission, no-progress policy, writer continuation,
  idle diagnostic and wait-style ready-work acceptance). It is stage-4
  userland: the runtime reads these names from configuration, not source.
- `observation_queues.toml` declares `vcs_observations` and `ci_observations`:
  bounded queues to which observation drivers append what they find. Their
  bodies are opaque plain objects; the schemas enforce bounds and required
  fields only. It is stage-5 userland. A config written by `mizu configure`
  already declares `ci_observations`, so including this fragment there would
  duplicate that key.
- `observation_drivers.toml` declares the illustrative `ci_costs` driver in
  `drivers/ci_costs_driver.py`. Including it switches the `vcs_read ci_costs`
  join to that operator-owned program through the generic host seam; without
  it the op fails closed. A config written by `mizu configure` already carries
  a working `ci_costs` declaration with an absolute, CWD-independent command
  pointing at a durable copy of the program in the config dir, so including
  this fragment there would duplicate the key. A manually authored or migrated
  config must copy the reviewed program into a durable location that survives
  upgrades (the config dir) and declare an absolute argv of its Python
  interpreter and that copy, as `mizu configure` generates; the relative argv
  here is illustrative only, not a usable deployment command.
- `policies/observations.md` says what an observation in each queue means, who
  reads it and what the reader does. Compose it into the role policies of the
  roles that read those queues.

`work` is only the stage-3 message form. The dedicated work-item store stays
authoritative until cutover; declaring the queue changes no runtime authority.

Declaring the observation queues changes no runtime authority on its own. The
separate driver declaration does switch the CI cost computation to an
operator-owned program; the remaining dedicated version-control and CI code
stays authoritative until later cutover and deletion slices. Only the example
driver command is shipped here; a deployment supplies its own.

Roles reach the queues with the `queues` capability and the `queue_*` and
`message_*` tools. The operator and host tooling use
`mizu queue PROJECT write|read|ack|message|refs` (see
[docs/operations.md](../../docs/operations.md)). Which queues wake which
roles is the `wake` list of each declaration; a reply also wakes the author of
the message it refers to.

To use it in a manually authored config, include the files and add the policy
part. For a config created by `mizu configure`, omit the two observation
fragments: its `ci_observations` queue and `ci_costs` driver are already
declared and including them again is a duplicate-key error.

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
