# Development userland (example)

This directory expresses a development workflow (proposals and their
decisions, questions and answers, notices, work and its completion, checks) as
queue declarations and role policy on the kernel
([docs/kernel.md](../../docs/kernel.md)). It is an example: a deployment
includes it from its own configuration and changes it freely. The kernel reads
nothing here specially.

`queues.toml` grows stage by stage as
[docs/os-migration.md](../../docs/os-migration.md) moves each workflow out of
the kernel code. A queue is added here in the same change that switches its
code path and deletes the dedicated module. `kinds.toml` belongs to the object
store that stage 1 replaces and is deleted with it.
