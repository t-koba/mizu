# ADR-008 — Upstream sync via trusted argv adapter and `sync` capability

**Accepted.** Add an operator-configured trusted argv `[vcs] command` adapter
speaking JSON on stdin/stdout (same shape discipline as `web.search_command`:
bounded request and response, timeout, no shell). The host side (not the
sandbox) uses it to fetch upstream refs and injects them read-only into the
managed workspace as `refs/remotes/upstream/*`. New capability `sync` lets the
writer merge or rebase `upstream/main` and resolve conflicts, then proceed with
normal verify and publish. The daemon schedule performs a periodic fetch.
Other human developers may commit upstream at any time.

## Context

Mizu projects are single-writer workspaces with content-addressed snapshots.
Upstream collaboration needs a bounded bridge to outside refs without giving
models host shell, network, or push rights. `web.search_command` already
establishes the trusted-argv JSON pattern; reuse it for VCS.

## Decision

- Mechanism provides capabilities; policy decides behavior. The `[vcs]`
  adapter, `sync` grant, read-only ref injection, and snapshot-digest
  exclusion are mechanism. When to fetch/merge, merge vs rebase choice, and
  conflict-resolution judgment are operator/worker policy, not code
  heuristics.
- `[vcs]` table: `command` (argv array, empty disables), `timeout_seconds`,
  `max_bytes`. Unknown keys fail validation. Defaults keep existing configs
  loading. Step 1 ships validation/docs only; fetch, ref injection, `sync`
  dispatch, and daemon fetch land in later steps behind the same bounds.
- Adapter contract (full M1): JSON stdin `{"op": ..., ...}` bounded by
  `max_bytes`; JSON stdout object with refs; `process.run` with timeout, no
  shell, `maximum=max_bytes`; nonzero exit, timeout, oversize, malformed JSON
  are `Denied`. Host-side only, never in sandbox, never model-provided argv.
- Refs appear read-only under `refs/remotes/upstream/*` in workspace views;
  snapshots exclude them from `code_digest` tampering (digest covers tracked
  source only). `sync` refused without grant; unknown op refused otherwise.
- Conflict flow: writer with `sync` merges/rebases `upstream/main`, resolves
  conflicts as normal workspace edits, then must pass normal `verify` and
  publish. No silent auto-resolve in mechanism.
- Daemon periodic fetch uses a fake-clock-testable interval; fetch failures
  are best-effort events, never silent publication.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: `[vcs]` as above; adapter JSON request/response documented in
  `docs/configuration.md` (this step) and later in the fetch module docstring.
- Bounds: argv entries nonempty without newlines; `timeout_seconds`,
  `max_bytes` in `[1, 16777216]`; adapter stdout capped at `max_bytes`.
- Trust: operator-owned host program; upstream refs are external-untrusted
  until merged and verified. No secret access, no host execution outside the
  trusted argv.
- Retry/cancellation: single invocation per fetch under the configured
  timeout; no shell retry. Daemon cancellation via stop event.
- Evidence: fetch receipts with adapter digest; `sync` results bound to
  resulting `code_digest` via normal verification.
- Failure: `ConfigError` on bad config; `Denied` on adapter shape/timeout/
  oversize/nonzero-exit; `Denied` on `sync` without grant.

## Consequences

Staged delivery: step 1 (config/docs/tests) lets operators stage `[vcs]`
without behavior change. Later steps add the adapter module, ref injection,
`sync` dispatch, and daemon fetch, each with contract tests (fake command:
success, timeout, oversize, malformed JSON, nonzero exit), read-only and
digest-exclusion tests, grant tests, end-to-end conflict test with fake
engine, and periodic-fetch test with fake clock. Offline `check.py` stays
green. No model-specific defaults; portable stdlib-only runtime.
