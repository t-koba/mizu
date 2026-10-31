# Child authority configuration digest (not integrated)

`engine_config.child_authority_digest` is a pure, unwired configuration-binding
helper. It returns only a SHA-256 string (64 lowercase hex characters). It does
not authorize execution, create a job, persist configuration, or change the
synchronous delegation path. Async runner integration remains disabled.

## Input contract

The trusted caller supplies already resolved inputs; resolution is deliberately
outside the pure helper. Never pass the session/conversation identity projection:
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

This helper binds configuration, not every mutable dependency or external grant.
Before any runner integration, the caller must establish the exact resolved
inputs used at launch and close these gaps, or refuse launch:

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
  comparison. Shared budget admission and workspace ownership require their own
  atomic mechanisms; a matching digest proves neither.

These are explicit integration prerequisites, not claims of complete effective
runtime authority, process/container quiescence, or pilot readiness. The focused
tests use synthetic values and local reviewed-content fixtures only.
