# Development userland (example)

This directory expresses a development workflow — proposals and their
decisions, work items with waits and completion evidence, checks — as kernel
configuration ([docs/kernel.md](../../docs/kernel.md)). It is an example: a
deployment includes it from its own configuration and changes it freely. The
kernel reads nothing here specially.

`kinds.toml` grows stage by stage as [docs/os-migration.md](../../docs/os-migration.md)
moves each workflow out of the kernel code. A kind is added here in the same
change that switches its code path to the kernel and deletes the dedicated
module.
