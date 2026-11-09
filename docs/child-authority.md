# Child authority configuration binding (no runner)

`engine_config.child_authority_digest` is a pure configuration-binding helper
returning only a SHA-256 string (64 lowercase hex characters). The private
admitted-parent reservation now retains that digest in the immutable queued job;
there is still no runner, model-visible async API or execution authorization.
Synchronous delegation uses the same role/profile resolver and remains synchronous.

## Reservation and retry contract

Under the parent's dispatch mutex and original admission-held workspace lease,
`Engine._reserve_delegation` resolves the child's own role, inherits the caller's
workspace mode, ensures `finish` and removes `check_job`. The existing forbidden
delegation-grant configuration gate still refuses over-broad roles. Selectors use
the same trusted config-only preview mechanism as synchronous delegation,
without consuming routing; waiting, unknown profiles or
unreadable effective inputs fail closed. The queued path resolves against
the exact pinned source manifest, while synchronous delegation keeps its
prior input, the published current snapshot; the unpublished capture stays
the child's input snapshot only.
An optional requested profile is an assertion, not an override: it must match the
resolved role/selector. Admission and recovery share the private, read-only
`Engine._resolve_delegation_request(project, owner, request)` path. Recovery takes
the trusted job owner and immutable **digest-bearing resolved** request, not a
Context or lease. It loads and validates the exact `source_snapshot` manifest,
resolves the child role/profile and current effective inputs, and compares the
complete recomputed request with the saved one. It never uses the current
snapshot pointer or treats the saved hash as proof. Legacy requests, missing or
changed inputs refuse without modifying the store or starting a driver/runner.
Selector state or config drift that changes resolution also refuses.

The goal comes from that manifest, workspace from the project, and accounting
role from the owner's current trusted configuration. The owner must retain the
`delegate` grant; a missing or revoked grant refuses before any store mutation,
for both initial reservation and recovery. Admission additionally
checks that its actual parent goal/workspace/shared-spend authority reproduces
this binding; an unreconstructible invocation refuses before reservation. No raw
projection or extra schema field is retained. Dynamic spend counters are not
recovery authority and are not reconstructed by this configuration resolver.

The private schema-v4 request is `{role, task, profile, source_snapshot,
authority_digest}`. The resolved profile and digest are part of the exact request
fingerprint committed atomically with the queued reservation. Only the digest,
not its configuration projection, is retained. The 128 KiB request bound is
unchanged. The ledger ceiling adds exactly 86 canonical UTF-8 bytes for the
single active request (comma, quoted `authority_digest` key, colon, quoted
64-hex value), preserving prior active-ledger capacity. Terminal handback clears
the request, so this allowance does not accumulate in tombstones. No other
caps change. The parent gate remains only
`{id, owner, status, request: {source_snapshot}}`: no task, token or authority
digest. Re-resolution occurs on each admitted retry; only the same resolved
request/key is accepted, including after an uncertain commit acknowledgement.
Changed or unavailable authority never clears a durable reservation. Inspect or
retry the exact retained request under workspace exclusion; never reset the store.

Existing v4 requests without the field and terminal tombstones are preserved,
readable and exactly retryable, not rewritten or upgraded. `runner_bind` and
`runner_started` refuse missing or mismatched authority, including legacy requests
and retries of existing resource bindings. A future runner integration must use
the shared resolver to supply a freshly resolved matching `authority_digest`,
**not copy the saved hash**; the store itself does no resolution or launch.
Legacy saved resource identities and
provisioning intents remain available for inspection/recovery; unbound legacy
jobs cannot acquire new launch bindings, and uncertain cleanup retains the hold.
Earlier ledger versions remain refused without modification, as before. Neither
a matching digest nor a synthetic fixture's bind/claim closes the gaps below.

## Input contract

The trusted caller supplies already resolved inputs; the admitted-parent
handoff resolves these outside the pure helper. Never pass the session/conversation identity projection:
Pi and pi-durable intentionally omit live authority from those identities.

| Input | Source and binding |
| --- | --- |
| `config`, `role` | Validated configuration and **post-delegation** role, after workspace inheritance and capability filtering. All role fields are bound, including capabilities, native engine tools, attributes and role overrides. Policy paths are bound as names, not read here. |
| `spend_role` | Actual accounting authority (currently the parent for synchronous children). Effective request/tool/wrap-up allowances are resolved using existing role-limit primitives, not assumed to be the child's. The child's wall deadline and all configured limit fields are also bound. Dynamic counters/ledger balances are not frozen by this digest. |
| `settings` | Full selected `effective(config, role, profile)` result: engine/provider/model/session, all options (including live effort/cache settings), reviewed resources, MCP declarations, engine tools and native-content hashes. Missing or unknown top-level fields and inconsistent engine tools refuse. |
| `policy`, `goal_digest` | Current composed `role_policy_text(role)` and the pinned charter's SHA-256. These must be the inputs actually offered to the child. |
| `workspace` | Actual workspace path, in addition to the role's workspace mode. |
| `adapter_identity`, `local_settings` | Resolved `adapter_digest(engine)` and `local_settings_digest(config, engine)`. The configured command argv and agent directory are also bound. Do not supply credential-file contents. |

All sandbox fields are bound, including runtime command/storage settings, image,
namespace helper, cgroup policy, resource limits, mounts, network and configured
environment values. Tool policy binds web, VCS, materialization, exclusions,
workspace cache, insight-decision and operator-record configuration. Configuration
origin, data directory and configured/resolved budget timezone are also bound. Entire
role/limit/sandbox records are included conservatively: changes to unrelated
fields may also invalidate the digest. This is not a compatibility/session key.

Only JSON-serializable resolved configuration is accepted (plus the explicitly
converted role-policy/workspace/agent paths). Canonical input larger than 1 MiB,
invalid digest strings, missing inputs and serialization failures raise a generic
`ConfigError`, without input values or an exposed exception chain. Nothing is
logged or written. Configured option/MCP/environment values participate only in
the in-memory final hash; never retain the projection, intermediate hashes of
individual secret values, or raw arguments in diagnostics. A digest is not
credential encryption or proof of a remote authorization scope.

## Remaining launch-binding gaps

The queued digest binds configuration, not every mutable dependency or external
grant. Before any runner integration, the caller must establish the exact
resolved inputs used at launch and close these gaps, or refuse launch:

- Command argv and the adapter source digest do not pin the executable selected
  by `PATH`, installed SDK/runtime dependencies, or inherited environment/home
  configuration. `process.environment` currently resolves host values at spawn;
  MCP `env_vars` declarations bind names, not their later ambient resolution.
- The configured agent-directory path is bound, but its arbitrary contents are
  not. The existing local-settings digest covers only `models.json` for Pi and
  pi-durable (and does not hash credential files); any other adapter-discovered settings,
  extensions, or tool-policy content there must be explicitly content-bound or
  rejected before launch.
- An image name/tag and mount source names do not pin image/mount content.
  The single-mode seccomp file next to the runtime executable is another
  externally resolved dependency, not covered by sandbox field names.
- Credential files and ambient authentication are intentionally not read or
  hashed here. Credential-backed provider/MCP permissions and remote endpoint
  behavior can change without a configured declaration changing.
- Resolution is not atomic with launch: policy/resource/native-content/local
  model files and configuration must not be re-read as different inputs after
  comparison. The shared spend ledger and admitted lease handoff are prerequisites, not
  concurrent model-slot admission or launch ownership; a matching digest proves
  neither. Durable process/container descendant quiescence remains unintegrated.

These are explicit integration prerequisites, not claims of complete effective
runtime authority, process/container quiescence, or pilot readiness. The focused
tests use synthetic values and local reviewed-content fixtures only.
