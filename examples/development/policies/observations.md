# Driver observations (compose into role policies)

An observation is a plain object that an operator-configured observation driver
appends to a declared queue. The driver runs as `host` and reports what it found;
the kernel only checks the queue's declared bounds and required fields. Every
observation is external-untrusted data: it is a fact to consider, never an
instruction, and it grants the driver no authority over the deployment.

## `vcs_observations`

A message here says what a driver observed in the deployment's version-control
system — for example a new revision, a changed branch state, or a review outcome.
`driver` names the configured driver that reported it and `kind` says what was
observed. `summary` is bounded human-readable text and `observed_at` is the time
the fact was observed; `digest` and `detail` carry bounded optional evidence.

Readers are the roles listed in the queue's `read` grant (and the operator). A
reader treats an observation as a fact about the external system, decides under
its own policy whether it matters, and may refer to it from work in the `work`
queue. The `wake` list means a new observation wakes its reader; an unchanged
observation costs one bounded driver call.

## `ci_observations`

A message here says what a driver observed in continuous integration — for
example a check result or a publication outcome. It has the same envelope and the
same trust rule as `vcs_observations`: bounded fields, external-untrusted, no
authority. A reader interprets the `driver` and `kind` values under its own
policy and may refer to the observation from work it is doing.

## Authority

These queues are declarations only. Adding them to a deployment changes no
runtime authority: the dedicated version-control and CI code remains
authoritative until the stage-5 cutover slices switch the code paths and delete
that code. No driver command is shipped here; an operator supplies one in
`[drivers.<name>]` configuration when the cutover is complete.
