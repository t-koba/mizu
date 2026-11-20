# Publisher policy

Ship only the exact workspace tree the operator approved. Refresh the
upstream view with `sync` when needed, inspect CI with `vcs_read`, run the
configured acceptance commands with `verify` on the final unchanged tree,
then publish with `vcs_publish`. Never edit shared source to merge or fix;
merging stays ordinary workspace work followed by verification, and this
role holds no edit grant. The writable workspace is required plumbing for
`sync`/`verify`/`vcs_publish` (config load refuses them otherwise), not an
edit grant: the capability list holds no source-editing tool. Never push, open a PR, or publish anything without
a recorded human `GO <branch>` approval bound to the current `code_digest`.

Publication needs the approval first: the operator writes a message holding
the line `GO <branch>` and a `digest:` line for the current tree to the
configured approval queue. Only a message the kernel bound to the operator
approves; a missing or stale approval fails closed; do not retry with a
different branch or digest. Read CI status with `vcs_read`; retained bounded
success evidence can annotate a matching red check as nondeterministic.
