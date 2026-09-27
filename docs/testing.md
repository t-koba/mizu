# Testing and acceptance

## Layered evidence

| Layer | Command | Proves | Does not prove |
|---|---|---|---|
| Offline core | `python3 scripts/check.py` | Files, locks, budgets, failure paths, JSONL, grants, MCP, escaped reports, bounded process behavior | Real Pi/provider/container behavior |
| Installer | `./scripts/test-install.sh` | Core staging, spaces, idempotence, manifests, CLI import/export/report/backup/restore | npm registry/install or model execution |
| Dependency install | `./scripts/setup.sh` | Exact installed Pi version and local staging gates | Live credentials or isolation |
| Real isolation | `mizu doctor --sandbox` | Actual rootless command boundary on that host | VM-grade isolation or kernel security audit |
| Live protocol (pi) | `mizu smoke --live` | Exact model selection, trusted extension handshake, admission hook, read + finish + settled | Research quality, all providers, large codebases |
| Live protocol (codex/claude) | `mizu smoke --live` with an `engine = "codex"`/`"claude"` profile | Trusted argv, isolated home/config, required MCP bridge, budget admission, finish round trip, no side effects | Host-tool absence (configuration, not proof), all models, quotas |
| Real work unit | Demo `run` with verification | Write, command execution, verification and publication together | Long-term reliability |
| Soak | Operator-observed 24-hour deployment | Behavior over the tested workload/window | General availability SLA |

`tests/fake_pi.py` is an intentionally local contract peer used by tests, not a
replacement Pi or production fallback. `tests/test_drivers.py` uses injected
fake runners the same way for codex/claude, plus a real-subprocess test of the
exact `mizu.mcp_proxy` stdio command the drivers hand to vendor CLIs. Podman argv tests use mocks where marked.
Node tests use a real local Unix socket but fake the Pi extension registry. A
pass count must retain those labels.

## Offline commands (portable: Linux / macOS / Windows)

```sh
python -m unittest discover -s tests -v
node --test tests/bridge.test.mjs
python scripts/check.py --report /private-validation/offline.json
```

On Linux additionally:

```sh
./scripts/test-install.sh
```

On Windows without bash use `python scripts/install.py --core-only ...` and
`bin\\mizu.cmd` instead of the `.sh` wrappers. `scripts/check.py` treats
missing bash as `not_run` for shell-syntax only; missing Node still fails
because the bridge contract needs it. Podman/systemd assertions never run
off Linux: they report `not_run`, not success.

No API key, npm dependency, browser or container daemon is needed for those
commands. Node tests run on the available modern Node test runner, while actual
Pi deployment still requires Node >=22.19.0. Python CI covers 3.11–3.13; only the
versions listed in a particular validation receipt were actually run there.

Test categories include path/symlink/hardlink refusal, immutable ID retries,
budget races and day changes, process cancellation/orphan pipes, unchanged
review skipping, goal/snapshot consistency, late Insight wakeup, stale verification,
source changes after verification, read-only Editor transport, unsafe ingest,
backup traversal/links/limits, hostile HTML, exact model mismatch, early Pi exit,
Unicode LF framing and agent_end vs agent_settled.

## Real-machine checklist

Use a disposable source copy and dedicated unprivileged account. Record release
SHA, dependency-lock SHA, Node/Python/CLI versions (Pi/Podman plus `codex`/`claude`
when those engines are configured), image digest, provider and
model IDs, engine per profile, container runtime and version, commands and sanitized evidence.
Do not store credentials in a public receipt.

Run doctor including the sandbox, then explicit live smoke. Execute the demo
work unit and inspect actual files and configured tests. Interrupt a bounded
command in a disposable project; confirm process groups/containers are gone,
the workspace remains, and restart does not silently replay. Try concurrent
workers and verify rejection. Lower request/time/output limits deliberately and
confirm refusal. Export an Editor capsule and attempt a write to its source;
confirm failure and successful proposal submission. Inspect artifact evidence
and stale snapshot labeling. Exercise backup/restore and staged update/rollback.

Then enable periodic roles with modest budgets. Observe an entire day, including
idle periods, paper generation, source refresh, provider failures and a restart.
Track request admissions, provider bills, container count, memory, disk growth,
iteration latency and usefulness of accepted changes. Use measured baselines;
no throughput or cost improvements are claimed without a reproducible workload.

Only after this evidence is reviewed should an operator mark a release as
accepted for that environment. An absent test is `not_run`, not a success.

### Single-ID hosts

Record additionally the wrapper path, the single `0 -> self` map observed by
doctor, the explicit storage paths, the delegated cgroup parent with its
enabled controllers, and the base-image digest reviewed before the build.
Prove the negative cases too: a multi-range map request against the wrapper
setup must fail, `doctor --sandbox` must fail when the cgroup parent is
missing, and an inherited `API_KEY` must fail the probe. Image-index
confusion after a failed build is recovered with `system reset` plus re-pull;
re-run `doctor --sandbox` afterwards and keep the recovery in the receipt.
