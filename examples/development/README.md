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

## One-time legacy conversion

`convert_legacy.py` is an operator-run example-userland command. It reads the
current legacy insights and their effective decisions through the public
Insight API and appends equivalent messages to the `proposals` and `decisions`
queues declared in `queues.toml`. Archived revisions stay in the legacy store;
nothing is rewritten and no runtime authority, route or capability changes.
Decision messages use the queue's declared writer principal (`role:writer`) as
historical authorship only. A deferral whose revisit carries an aware instant
also becomes a delayed decision message (same action and reason, referencing the
converted proposal) due at exactly that instant; past instants are immediately
due. A revisit with no instant has no delayed message, and a malformed one
refuses the run before any append.

```sh
python3 examples/development/convert_legacy.py config/config.toml --project NAME [--apply]
```

Message ids are derived from the legacy insight id and revision, plus a
separate domain for revisit messages, so a repeat appends nothing. Both the dry
run and `--apply` report `proposals`, `decisions` and `revisits` id lists.
Without `--apply` the command is a dry run: it prints the planned ids and writes
nothing. Malformed or ambiguous legacy input refuses the whole run before any
append.
