# Testing and acceptance

## Layered evidence

| Layer | Command | Proves | Does not prove |
|---|---|---|---|
| Offline core | `python3 scripts/check.py` | Files, locks, budgets, failure paths, JSONL, grants, MCP, escaped reports, bounded process behavior | Real Pi/provider/container behavior |
| Installer | `./scripts/test-install.sh` | Core staging, spaces, idempotence, manifests, CLI import/export/report/backup/restore | npm registry/install or model execution |
| Dependency install | `./scripts/setup.sh` | Locked Pi dependency install and local staging gates | Live credentials or isolation |
| Installed SDK / mock provider | `node scripts/check-pi-sdk.mjs` and `python3 scripts/check-model-adapters.py --help` | Current public SDK, runtime metering, managed bridge/MCP, Codex/Claude resume and usage deltas | Paid inference or OCI isolation |
| Real isolation | `mizu doctor --sandbox` | Actual rootless command boundary on that host | VM-grade isolation or kernel security audit |
| Live protocol (pi) | `mizu smoke --live` (exact-path `probe.txt` goal plus fixed probe policy, fixed 4-request / 5-tool / 120 s cap) | Exact model selection, trusted extension handshake, admission hook, read + finish + settled | Research quality, all providers, large codebases |
| Live protocol (codex/claude) | `mizu smoke --live` with an `engine = "codex"`/`"claude"` profile | Trusted argv, isolated home/config, required MCP bridge, budget admission, finish round trip, no side effects | Host-tool absence (configuration, not proof), all models, quotas |
| Real work unit | Demo `run` with verification | Write, command execution, verification and publication together | Long-term reliability |
| Soak | Operator-observed 24-hour deployment | Behavior over the tested workload/window | General availability SLA |

`tests/fake_pi.py` and fake runners in `tests/test_drivers.py` are contract test doubles, not production fallbacks. Tests verify argv construction, stdio MCP framing (`mizu.mcp_proxy`), and error handling without contacting live endpoints. Node tests use a local Unix socket with a simulated extension registry.

## Offline test execution (Linux / macOS / Windows)

```sh
python3 -m unittest discover -s tests -q
node --test tests/bridge.test.mjs
python3 scripts/check.py --report private-validation/offline.json
```

On Linux:

```sh
./scripts/test-install.sh
```

`scripts/check.py` writes a versioned offline receipt (`report_version`,
`mizu_version`, `source_sha256` over contents plus executable bits, exact
`python`/`os`, exact `node_version`, canonical sorted `allowed_not_run` from
the CLI, functional `bash_available`, `ok`, and the full `checks` array). The
source digest is captured before validation and compared again after all checks;
`source-manifest-stable` must pass, otherwise the report fails. JavaScript
syntax check names include source-relative paths to avoid basename collisions.
Core-only installation may reuse that receipt instead of running the nested
staged gate a second time, only via an explicit handoff:

```sh
python3 scripts/check.py --report private-validation/offline.json --allow-not-run node-durable-contract-tests,node-pi-sdk-contract-tests,node-pi-reasoning-replay-tests
./scripts/test-install.sh private-validation/offline.json
```

Reuse is explicit and content-bound: `scripts/install.py --reuse-source-checks`
is allowed only with `--core-only`, binds to the exact shared distribution
source-manifest digest, exact Python version and OS, the versioned report
schema, `ok: true`, passing `python-syntax`, `python-unit-and-contract-tests`,
`node-transport-and-extension-tests`, `source-manifest-stable`, and the source
tree's JavaScript/shell syntax checks. All three Pi SDK gates must be present,
with either `pass` or an allowed `not_run` result: a real SDK pass is accepted.
The requested `allowed_not_run` must exactly equal the sorted
`staging_not_run_checks(core_only=True)` waiver set, even when SDK gates pass.
The current Node version and functional Bash availability must also match the
report. No other `not_run` is accepted except generic `shell-syntax` when the
report records no functional Bash. Duplicate check names are rejected. Explicit
reports must be regular files of at most 1 MiB, read boundedly before parsing.
Stale, malformed, failed, incomplete, wrong-environment, or unexpected-waiver
reports are rejected, and the staged-manifest equality check still runs. Without an
explicit receipt the installer runs its full staged gate unchanged. The
installation receipt records `checks_provenance` as `reused-source-gate` or
`staged-gate` so reused evidence is never presented as having run inside the
staged tree. Never substitute the general `offline.json` receipt implicitly;
the CI core-installer step passes the prior report path explicitly, while a
plain local `./scripts/test-install.sh` still executes the staged gate.

On Windows without bash: run `python scripts/install.py --core-only ...` and test via `bin\mizu.cmd`. Missing bash marks shell-syntax checks as `not_run`; missing Node fails the suite. The Pi SDK, durable-contract, and reasoning-replay gates need the pinned adapter dependencies: absent dependencies fail closed with the provisioning remedy unless their exact gate is explicitly waived; a version mismatch always fails. Installer staging records mode-specific `not_run` waivers: core-only stages waive the Pi SDK, durable-contract, and replay gates because neither adapter is provisioned; full Pi stages run the Pi SDK contract gate against the installed `adapters/pi` and waive only durable-contract and replay, because `pi-durable` is not installed. The required `durable-contract` CI job provisions both pinned adapters from committed lockfiles in a network-enabled setup step, then runs all gates unwaived and offline; the CI matrix and installer-staging waivers are scoped fallbacks, not substitutes for that authoritative job. Podman and systemd validations only run on Linux.

Offline tests require no API credentials, internet access, or running container daemons. The CI matrix targets Python 3.11–3.14; the recorded local result is Linux 3.14.

Test categories include path/symlink/hardlink refusal, immutable ID retries,
budget races and day changes, process cancellation/orphan pipes, unchanged
review skipping, goal/snapshot consistency, late Insight wakeup, stale verification,
source changes after verification, read-only Editor transport, unsafe ingest,
backup traversal/links/limits, hostile HTML, exact model mismatch, early Pi exit,
Unicode LF framing and agent_end vs agent_settled.

### Atomic state read/replace contract

`read_json` opens managed state with Windows delete sharing (a `CreateFileW`
handle transferred to a Python stream). Non-exclusive Windows publication uses
`SetFileInformationByHandle(FileRenameInfoEx)` with `FILE_RENAME_INFO.Flags` set
to `REPLACE_IF_EXISTS | POSIX_SEMANTICS` (0x1 | 0x2); delete sharing alone does
not make `os.replace` work with a retained target reader. The source handle has
`DELETE` access and the destination name is absolute UTF-16 with a byte length.
Unsupported native operations and sharing/access errors fail immediately: no
fallback or retry window. Exclusive create-only publication still uses
non-replacing `os.rename`; POSIX publication still uses `os.replace`. For files
published by `atomic_write`, a retained product reader must read complete A
while publication and new product reads complete B. External Windows readers
that omit delete sharing can still block replacement; permission/sharing errors
propagate rather than becoming missing state. This is not the untrusted-path
validation contract of `safe_read`, nor a guarantee for in-place writes.

`FileTests.test_atomic_replace_with_retained_reader` holds A behind events until
B is published and read, and checks create-only idempotence/conflicts before
releasing A. It is included in the ordinary CI matrix; Windows semantics require
an actual Windows result for the exact revision, not a Linux pass or mocked
Win32 calls. `WindowsReplaceTests` checks the native wrapper's flags, buffer
layout (including non-BMP UTF-16), error propagation and handle cleanup using
mocked calls; it does not establish actual filesystem semantics. API references:
[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle),
[FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info),
and [rename flags](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information).
To run the focused file suite on Windows:

```powershell
python -m unittest discover -s tests -p test_primitives.py -v
```

### Windows per-test timing evidence

Timing artifact emission is explicit, independent of `--report`:

```sh
python scripts/check.py --test-timings private-validation/windows-test-timings.json
```

Ordinary local `python3 scripts/check.py` creates no timing artifact. The child
(`scripts/check_unittest.py`) flushes a value-free temporary JSONL sidecar per
test: stable test ID, start/stop event, and monotonic offset only; the parent
derives durations. Parent and child share a system-wide monotonic clock origin (Python 3.11+ on supported
platforms); absolute clock values are never exported. An unmatched start records
elapsed duration at child exit or timeout, before process cleanup, with
`completed: false`; completed entries use the matching start-to-stop duration.
Malformed/unmatched stops and invalid durations are ignored.

Writer and parser share bounds of 1,000,000 bytes, 20,000 lines/tests and
100,000 seconds per offset/duration. The writer reserves each start/stop pair
before admission and stops collecting when full, preserving the parseable prefix
rather than growing past the parser's cap. Malformed lines are skipped; missing,
empty or oversized sidecars yield no timing entries. Collection, parsing and
artifact writing are best-effort diagnostics only, never a test pass/failure
signal. There is no retry protocol; temporary sidecars are removed after use.

The dedicated artifact contains only sanitized test IDs,
`duration_seconds`/`completed` flags, and minimal runtime metadata (suite,
versions, platform, child timeout, total suite elapsed time, gate status). It never
carries failure details, output tails or diagnostics; do not substitute the general
`offline.json` receipt. Only the Windows-latest/Python 3.13 CI step supplies
`--test-timings`; CI uploads only this dedicated file for that job (`actions/upload-artifact` v4.6.2, 7-day retention,
`if: always()` with missing-file tolerance). It records diagnostic evidence; it
sets no timeout policy. The original Windows scope is 40 minutes with a 1450 s
child cap. The modeled setup/child/later-check/cleanup/suffix path is 2296 s,
leaving 104 s inside the job; this arithmetic is a contract, not a worst-case
runtime claim. The historical run reached `test_vcs_ci` at the child cap without
a reported test failure. If the bounded run stalls again, use its partial
per-test artifact to identify the unfinished test; timing samples are not bounds.

### Windows hotspot inventory (bounded, evidence-only)

Source: Windows/Python 3.13.16 artifact
`windows-test-timings/windows-test-timings.json` for `mizu` SHA
`1ea8ddb21fc9a3dc334a522465deeb114b04036f`: 1,922 tests passed; per-test
durations sum to 967.354 s and unittest elapsed time was 969.41 s. These
measurements are diagnostic evidence for that SHA only, not exact-SHA
evidence for the current workspace. This inventory changes no tests, coverage,
timeout limits, or CI matrix; it is documentation only. Each row was written
from the named test plus neighboring tests in the same file. Where no
equivalent test could be proven from code, the row says so; no retirement is
proposed for any row.

| # | Test (measured s) | Uniquely protected behavior | Overlap / unique note |
|---|---|---|---|
| 1 | `test_recovery_probes.RecoveryProbeTests.test_long_running_probe_events_and_ordinary_successes_stay_bounded` (8.997) | Probe event history prunes at 8 entries across 9 failures; 100 ordinary successes add at most 256 bytes (under 32 KiB). | No demonstrated overlap for the pruning/size-bound assertion; `RecoveryProbeTests.test_terminal_contribution_retention_keeps_active_and_latest_receipts` covers a different retention limit. |
| 2 | `test_progress_admission.ProgressAdmissionTests.test_explicit_wakes_dispatch_on_change_writer_with_unchanged_code` (8.870) | `on_change` writer with unchanged code dispatches on operator/inbox/goal/elapsed-wait wakes; file-trigger dispatch still requires a due event; follow-up cannot loop. | Related individual behaviors appear in `test_new_inbox_generation_offered_to_run_counts_as_input`, `test_operator_wake_offered_to_run_counts_as_input`, and `test_external_wakes_and_wait_expiry_survive_parking`; unique in combining the wakes with the unchanged-code plus `event_trigger` gate. |
| 3 | `test_insight_pages.InsightPageTests.test_complete_live_ties_and_replay_over_1000` (7.761) | Live pagination over 1008 rows: same `created_at`/id ties break by path with no missing rows, cursor replay is deterministic. | No demonstrated overlap for >1000-row live pagination/tie behavior; neighbors cover staleness, archive, and expiry. |
| 4 | `test_remaining.RemainingTests.test_write_crash_and_capacity_matrix_recovers_published_state` (7.180) | Fault-injection matrix (EIO/ENOSPC/crash) at every publication boundary still recovers a readable published snapshot. | No demonstrated overlap; the subprocess per-boundary crash matrix is unique in the file. |
| 5 | `test_work_items.WorkItemTests.test_revision_bound_includes_seed` (6.090) | Revision bound counts the seed: reads at `MAX_REVISIONS` hold, writes past it are denied and unreadable. | No demonstrated overlap for the seed-inclusive bound; neighboring revision tests cover stale/malformed writes, not this bound. |
| 6 | `test_vcs_publish.PublicationReceiptTests.test_invalid_responses_remain_unresolved_and_never_redispatch` (5.723) | 14 malformed adapter responses stay `unknown`, keep the dispatch file, write no receipt, and retries are refused without calling the adapter. | Partial overlap with `PublicationReceiptTests.test_receipt_failure_is_unresolved_and_receipt_precedes_observation`; unique in the 14 invalid-response variants plus retry refusal. |
| 7 | `test_recovery_probes.RecoveryProbeTests.test_budget_denial_locks_and_controls_do_not_spend` (5.543) | Budget/lock/unarmed/paused/draining/stopped denials spend nothing (`driver.calls == []`) and keep the block active. | No demonstrated overlap for the combined denial matrix; neighbors cover launch propagation, not these denials. |
| 8 | `test_recovery_probes.RecoveryProbeTests.test_fresh_launch_limits_and_budget_reach_context_and_driver` (5.336) | Reloaded limits/timezone propagate to the launched config, driver context, and the budget file zone. | No demonstrated overlap for fresh-config propagation; neighboring tests cover revocation, not propagation. |
| 9 | `test_vcs_mutation_intent.MutationIntentTests.test_local_refusals_leave_no_intent_and_do_not_dispatch` (5.164) | Six local refusal reasons across all mutation cases write no intent file and never call the adapter. | No demonstrated overlap; sibling intent tests cover post-intent paths, not this pre-dispatch refusal matrix. |
| 10 | `test_vcs_mutation_intent.MutationIntentTests.test_adapter_exception_interruption_and_invalid_observation_leave_unknown` (5.075) | Adapter OSError/interrupt/cancelled/empty-observed outcomes stay `unknown` with the intent file equal to pending; close/refusal cannot retry. | Partial overlap with `MutationIntentTests.test_observation_write_failure_preserves_intent_even_after_replace` on refusing retries after uncertainty; unique in the adapter exception/interruption and invalid-observation matrix. |
| 11 | `test_deadlines.DeadlineTests.test_native_query_and_turn_progress_cannot_renew_absolute_deadline` (4.865) | Stalled native claude/codex requests fail on the 1 s logical bound without setting or renewing the absolute deadline. | Partial overlap with `DeadlineTests.test_stalled_durable_process_uses_host_request_not_global_wall`; unique in the native Claude/Codex request bound with no absolute deadline. |
| 12 | `test_materialize.MaterializeApplyTests.test_apply_utf8_byte_limit_refused_and_leaves_file` (4.342) | UTF-8 byte (not character) limit refuses apply, leaves the target bytes intact, and leaves no temp file. | No demonstrated overlap; neighboring materialize tests refuse other reasons, not byte-limit atomicity. |
| 13 | `test_deadlines.DeadlineTests.test_hung_and_trickling_channel_are_host_bounded_even_without_wall` (4.331) | Hung sleep and trickling-heartbeat channels both fail bounded (<3 s) with no wall policy, and the process is reaped. | Partial overlap with `DeadlineTests.test_stalled_durable_process_uses_host_request_not_global_wall`; unique in channel receive handling for the hung-plus-trickling pair. |
| 14 | `test_recovery_probes.RecoveryProbeTests.test_group_profiles_and_newer_failures_are_independent` (4.203) | Probe blocks are per-group-profile; a newer failure wins over a stale recover, and decisions rebind to `primary`. | Partial overlap with `RecoveryProbeTests.test_fixed_and_probe_overlap_both_orders`; unique in cross-profile independence plus the newer-failure/stale-recovery rule. |
| 15 | `test_recovery_probes.RecoveryProbeTests.test_non_success_results_keep_the_block` (4.176) | Seven non-success driver outcomes (denied/unsettled/challenge/target/failure/cancel/protocol-error) all keep the block. | No demonstrated overlap for this driver-outcome matrix. |
| 16 | `test_recovery_probes.RecoveryProbeTests.test_near_limit_admitted_probe_persists_terminal_and_recovery` (4.145) | Near-`MAX_BYTES` admitted probe persists terminal success and recovery within the cap with no `reserve` leak. | Partial overlap with `RecoveryProbeTests.test_byte_capacity_and_corrupt_event_fail_closed` for capacity refusal; unique as the near-limit terminal-success and recovery path. |
| 17 | `test_source_continuation.SourceContinuationTests.test_receipt_hash_and_kind_are_verified` (3.923) | Tampered receipt URL and probe-kind mismatch are both denied for `raw_source`. | No equivalent overlap was demonstrated in the inspected suite. |
| 18 | `test_ready_work.ReadyWorkTests.test_dispatch_unchanged_and_event_gates_do_not_suppress_ready_work` (3.907) | Ready work dispatches with unchanged code under both `event_trigger` values across done/wait/continue outcomes (`admission == ready_work`). | No equivalent overlap was demonstrated; the `ready_work` admission gate differs from progress-admission gates. |
| 19 | `test_work_item_events.WorkItemEventTests.test_closed_cli_exact_receipts_outside_summary_sample` (3.551) | Closed CLI reports exact per-role receipts beyond the 10-id summary sample and performs no file writes. | No demonstrated overlap for the CLI-plus-read-only-plus-exact-receipt combination. |
| 20 | `test_vcs_mutation_intent.MutationIntentTests.test_observation_write_failure_preserves_intent_even_after_replace` (3.388) | Early/late observation-write failures preserve the single intent file equal to pending; close/refusal cannot retry. | Partial overlap with `MutationIntentTests.test_adapter_exception_interruption_and_invalid_observation_leave_unknown` on preserving uncertain intent; unique in the early/late observation-write failure timing matrix. |


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
and stale snapshot labeling. Exercise backup/restore and staged update and removal of replaced code.

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
