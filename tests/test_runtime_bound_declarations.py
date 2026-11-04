"""First runtime-bound inventory, not a general numeric-literal linter.

The `src/mizu` files below cover named, direct module-level Assign/AnnAssign
bounds and direct numeric literal/arithmetic values at a short resource-call
keyword set. The gate inventory covers `scripts/check.py`'s host-cost, output
and artifact bounds; its enforced host-cost choices are configured policy
defaults, while its scope reserves and output/artifact bounds are fixed safety
ceilings.
The `vcs.py` provider response-shape bounds are fixed mechanism and safety
ceilings for untrusted content; their audit found no host-cost policy bound.
The `project.py` git-tree import time bounds are configured policy; its
remaining output caps are fixed safety ceilings. The `insights.py` module
constants are fixed record and evidence safety bounds; its constructor policy
defaults are named config defaults already inventoried or are supplied by
`Project`, including the configured malformed-receipt diagnostic sample. The
`runtime.py` literals are fixed run-local memory, one-row probe and public page
bounds; its configurable admission and dispatch choices are `Limits` and
`[dispatch_evidence]` fields, including the run-local queue-observation sample.
`evidence_records.py` declares the started.json authorization-read ceiling that
also trims that sample; runtime re-exports it only as an alias.
The `acceptance.py` retained-evidence ceiling and the `ci_observation.py`
shared-cache caps and `ci_waits.py` diagnostic page caps are fixed safety
bounds; `ci_waits.py`'s serialized check-set size is a mechanism contract;
`ci_wake.py`'s re-wake default is configured policy owned by
`[vcs] ci_rewake_seconds`, and its record version is format, outside the
name-scoped detector.
The `lock_diagnostics.py`
constants are fixed journal-line row and numeric-field format bounds; the
opt-in collection is policy, but it introduces no host-cost numeric bound.
The `cli.py` module-level inventory is empty; its numeric page defaults remain
explicit command-line presentation flags, not runtime bounds. The `config.py`
module-level candidates are closed over the existing policy/default
dictionaries and `CONFIG_MODULE_BOUNDS`: the include, role-policy, resource,
CI-wake-branch and workspace-depth ceilings are fixed mechanism or safety
bounds, and the wait-seconds value is the protocol format bound.
The `work_items.py` module-level page, cursor, row and revision bounds are
fixed mechanism or safety contracts; its page-byte aliases reuse those
declared bases rather than introducing separate literals.
The `fs.py` and `platform.py` framing, read-page, script-input, JSON-depth,
lock-wait and spawn-reap bounds are fixed mechanism or safety. The widened
character-count and maximum suffixes match the read-page and script-input names,
so they are ordinary declarations; `EXTRA_CANDIDATES` currently holds only the
probe header-pair count, whose name falls outside the pattern. Lock waits stay
fixed until the scheduled-contention work proves a replay-safe boundary.
Container cleanup and systemd-analyze verification walls are host-dependent
policy pending container-runtime measurement; their output reads are safety.
The `delegation_jobs.py` byte ceilings are private format/safety limits for the
not-yet-wired single-child delegation pilot. Its 1024-job history cap is
shared project-state capacity policy pending the pilot's configuration section:
capacity exhaustion refuses new starts rather than forgetting keys.
The `storage.py` artifact-retention default is CLI-controlled policy, while the
day conversion and automatic-prune/audit read limits are fixed mechanism or
safety bounds. The `drivers.py` event-stream default is the configured
`[limits] event_stream_bytes` policy default; its per-record transport ceiling,
stderr diagnostics tail and error-evidence tail are fixed safety bounds. The
character-count and maximum suffixes are part of the inventory so
operator-facing text and protocol limits are classified too, including the
publication reference format and the probe header/media bounds.
The `doctor.py` probe wall deadlines are configured policy owned by `[doctor]`;
its probe output reads remain fixed safety. The `work_order.py`, `selection.py`,
`continuations.py`, `startup.py`,
`smoke.py` and `probes.py` inventories are fixed mechanism or safety bounds:
the work-order page/rationale/record caps, selection list/state/reservation/
retry caps, continuation chain and pointer/run reads, ADR-042 host-relative
startup floor/multiplier/ceiling and probe/record caps, the ADR-006/ADR-013
paid smoke wall cap, and the recovery-probe execution ceilings. Selection
retention is the exception: `MAX_TERMINAL_CONTRIBUTIONS` and
`MAX_PROBE_EVENTS` are `configured policy` defaults for the optional
data-dir `selection/policy.json`; absence keeps 256/8 and malformed values
refuse selection. The
selection module's `DEFAULT_ON_ERROR_RETRIES` and `DEFAULT_CLASSIFIER_INPUTS`
are configured schema defaults declared separately; the per-rule and
per-classifier tables are their owning configuration. Every current
`src/mizu` module with a detected candidate is covered here (`config.py` has
its own field-scoped table), so a new module with such a bound fails until it
is declared. Every module with a detected numeric argument at the listed
resource-call keywords is covered in `CALLS` too, so a new host deadline or
read bound at those call sites fails until it is declared. The inventory
remains name- and numeric-scoped and therefore partial; absence from the tables
is not evidence that a value is not a decision. Expressions are inventoried
without evaluating source. This does not lint arbitrary numeric literals,
aliases, nested declarations, call arguments outside the listed keyword set,
local arithmetic or other modules. Expand coverage as the behavior-based
inventory reaches other owners. The configured slice checks Usage, SchedulerEvidence,
CiRequestEvidence, PublicationPolicy, PublicationEvidence and WorkspaceCache field defaults,
VCS polling fallback defaults, the retained-check Limits field and ceiling, and explicitly
listed raw web and VCS settings; the decision-history recovery ceiling is checked alone,
not as a full work_items.py inventory. Validation is covered
by focused config tests. Classification records current ownership, not
approval to leave policy in code. `format` is reserved for intrinsic wire or
schema ranges; `safety` is a defensive ceiling on untrusted or oversized input,
including a product-chosen validation maximum; `mechanism` is a fixed internal
or public contract, including a retry-loop bound; `policy` is an operator
decision not yet configurable; `configured policy` is its migrated form: an
operator-configurable setting or config-owned default with a documented default
and, where a field exists, a validated range. The declaration tables assert the
field or default expression, and the focused config tests exercise the range.
Safety classifications here are defensive read or validation bounds, not
intrinsic format limits.
"""
import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'src' / 'mizu'
GATE_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
CANDIDATE = re.compile(
    r'(?:MAX_\w+|DEFAULT_\w+|\w+_(?:LIMIT|CAP|TIMEOUT\w*|SECONDS|DAYS|DEPTH|'
    r'ENTRIES|BYTES|ROWS|COUNT|MULTIPLIER|PER_RUN|CHARS|MAX))\Z'
)
# Module-level names outside CANDIDATE that still bound public resource behavior.
EXTRA_CANDIDATES = {
    'web.py': frozenset({'PROBE_MAX_HEADERS'}),
}
# Non-numeric module-level policy defaults checked by their exact literal value.
POLICY_DEFAULTS = {
    'selection.py': {
        'DEFAULT_CLASSIFIER_INPUTS': (
            {'max_proposals': 8, 'max_decisions': 8, 'max_text_bytes': 2048},
            'configured policy',
            'selection / per-classifier inputs defaults; operator overrides through the '
            'classifier inputs table, documented in docs/model-selection.md'),
    },
}
# name: (exact ast.unparse(value), classification, owner / rationale)
DECLARATIONS = {
    'acceptance.py': {
        'MAX_BYTES': (
            '1048576', 'safety',
            'acceptance / fixed retained-evidence read and canonical-value ceiling; '
            'limits.file_bytes may narrow reads'),
    },
    'evidence_records.py': {
        'MAX_STARTED_EVIDENCE_BYTES': (
            '65536', 'safety',
            'evidence records / fixed started.json authorization-read ceiling; file_bytes may lower it'),
    },
    'ci_observation.py': {
        'MAX_CI_FAILURES': (
            '32', 'safety',
            'CI observation / bounded flattened current-failure rows for the shared cache and prompt'),
        'MAX_CI_CHECKS_PER_BRANCH': (
            '16', 'safety',
            'CI observation / bounded stored check rows per branch and per tick'),
        'MAX_CI_FAILURE_BRANCHES': (
            '128', 'safety',
            'CI observation / bounded stored branches with failure or success evidence'),
    },
    'ci_waits.py': {
        'MAX_CHECK_SET_BYTES': (
            '2048', 'mechanism',
            'CI waits / serialized public check-set contract bound'),
        'MAX_DIAGNOSTIC_ITEMS': (
            '10', 'safety',
            'CI waits / bounded diagnostic wait rows per projection'),
        'MAX_DIAGNOSTIC_ROWS': (
            '32', 'safety',
            'CI waits / bounded observed-check rows per diagnostic projection'),
    },
    'delegation_jobs.py': {
        'MAX_JOBS': (
            '1024', 'policy',
            'async delegation / shared project-state job-history capacity; lifetime tombstones '
            'are retained and exhaustion refuses new starts, pending pilot configuration'),
        'MAX_STATE_BYTES': (
            '1024 * 1024', 'safety',
            'async delegation / bounded ledger read; oversized state is refused, never reset'),
        'MAX_REQUEST_BYTES': (
            '128 * 1024', 'safety',
            'async delegation / bounded canonical request record'),
        'MAX_RESULT_BYTES': (
            '32768', 'safety',
            'async delegation / bounded untrusted result text'),
        'MAX_TASK_BYTES': (
            '65536', 'safety',
            'async delegation / bounded task field within the request record'),
    },
    'ci_wake.py': {
        'DEFAULT_CI_REWAKE_SECONDS': (
            '1800', 'configured policy',
            'VCS CI wake / fallback default for [vcs] ci_rewake_seconds; config validates 60-86400'),
    },
    'fs.py': {
        'PREVIEW_BYTES': (
            '131072', 'safety',
            'fs / fixed preview, insight and canonical-list framing; file_bytes and '
            'snapshot_bytes remain policy'),
        'READ_PAGE_CHARS': (
            '8192', 'safety',
            'fs / per-response read page character cap; keeps escaped responses below '
            'the frame bound'),
        'MAX_FRAME': (
            '1024 * 1024', 'mechanism',
            'fs / shared MCP and socket LF frame bound across bridge and editor server'),
        'MAX_JSON_DEPTH': (
            '64', 'mechanism',
            'fs / parser nesting bound; independent of the runtime recursion limit'),
        'SCRIPT_MAX': (
            '65536', 'mechanism',
            'fs / shared tool-contract and sandbox script input bound'),
        '_LOCAL_BLOCKING_TIMEOUT': (
            '30.0', 'mechanism',
            'fs / ADR-026 intra-process lock-wait bound; fixed until scheduled-contention '
            'work proves a replay-safe boundary'),
    },
    'platform.py': {
        'LOCK_TIMEOUT': (
            '30.0', 'mechanism',
            'platform / ADR-026 cross-process lock-wait bound; fixed until '
            'scheduled-contention work proves a replay-safe boundary'),
        '_SPAWN_REAP_TIMEOUT': (
            '5.0', 'safety',
            'platform / bounded reap of a process that failed secured startup'),
    },
    'drivers.py': {
        'DIAGNOSTICS_TAIL_BYTES': (
            '256 * 1024', 'safety',
            'drivers diagnostics / bounded stderr tail retained as failure evidence'),
        'EVENT_STREAM_BYTES': (
            '4 * 1024 * 1024', 'configured policy',
            'drivers event stream / default for [limits] event_stream_bytes; the configured value wins'),
        'EVENT_RECORD_BYTES': (
            '1024 * 1024', 'safety',
            'drivers event stream / one larger JSONL record fails closed instead of exhausting memory'),
        'ERROR_TAIL_CHARS': (
            '4000', 'safety',
            'drivers failure evidence / bounded error-text tail attached to driver errors'),
    },
    'storage.py': {
        'DEFAULT_KEEP_ARTIFACTS': (
            '30', 'configured policy',
            'storage prune / default artifact retention when --keep-artifacts is omitted; '
            'an explicit CLI value wins'),
        '_DAY_SECONDS': (
            '86400', 'mechanism',
            'storage retention / seconds-per-day unit conversion, not a retention choice'),
        '_AUTOMATIC_PRUNE_STATE_BYTES': (
            '4096', 'safety',
            'storage automatic prune / bounded due-state read; larger state is unavailable'),
        '_AUDIT_JSON_BYTES': (
            '1048576', 'safety',
            'storage audit / bounded reference-JSON read; larger records are skipped and counted'),
        'AUTOMATIC_PRUNE_INTERVAL_MAX': (
            '2678400', 'safety',
            'configuration validation / maximum accepted automatic-prune interval (31 days)'),
    },
    'usage.py': {
        'MAX_ATTEMPT_USAGE_KEY_CHARS': ('256', 'safety', 'usage / fixed stored-key label length ceiling'),
    },
    'dispatch_evidence.py': {
        'MAX_TEXT': ('256', 'safety', 'dispatch evidence / fixed persisted text-label length ceiling'),
    },
    'cli.py': {},
    'workspace_cache.py': {},
    'ci_requests.py': {
        'RECORD_BYTES': ('8192', 'safety', 'CI requests / bounded evidence-record read'),
    },
    'publication.py': {
        'RECORD_BYTES': ('8192', 'safety', 'publication / bounded evidence-record read'),
        'LINEAGE_JSON_BYTES': ('4096', 'safety', 'publication / bounded caller-supplied lineage JSON'),
        'REFERENCE_CHARS': (
            '256', 'format',
            'publication receipt / operator approval and run reference text limit'),
    },
    'run_health.py': {
        'MAX_RATE_FILE_BYTES': ('64 * 1024', 'safety', 'run health / bounded untrusted result-record read'),
        'MAX_RATE_BYTES': ('8 * 1024 * 1024', 'safety', 'run health / total status-rate content-read ceiling'),
    },
    'web.py': {
        'SEARCH_RESULT_LIMIT': ('1000', 'mechanism', 'web search / retained-result ceiling shared with the tool page bound'),
        'PROBE_MAX_HEADERS': ('16', 'mechanism', 'probe / caller header-pair count bound'),
        'PROBE_HEADER_NAME_MAX': ('128', 'mechanism', 'probe / caller header-name character bound'),
        'PROBE_HEADER_VALUE_MAX': ('4096', 'mechanism', 'probe / caller header-value character bound'),
        'PROBE_RESPONSE_HEADERS_MAX': ('64', 'mechanism', 'probe / retained response-header count'),
        'PROBE_RESPONSE_NAME_MAX': ('256', 'mechanism', 'probe / retained response-header name bound'),
        'PROBE_RESPONSE_VALUE_MAX': ('8192', 'safety', 'probe / retained response-header value bound'),
        'RESPONSE_MEDIA_MAX': ('256', 'safety', 'web fetch/probe / media-type label character bound'),
    },
    'project.py': {
        'MAX_VERIFY_COMMANDS': ('32', 'safety', 'project / bounded verification command list'),
        'MAX_RECORDED_BATCHES': ('32', 'safety', 'project / bounded named batch definitions'),
        'MAX_RECORDED_BATCH_COMMANDS': (
            '32', 'safety', 'project / bounded commands per named batch'),
    },
    'insights.py': {
        'MAX_DECISION_EVENT_BYTES': (
            '1024 * 1024', 'safety', 'insights / fixed decision-event record read and write ceiling'),
        'DECISION_RECEIPT_BYTES': (
            '1024', 'safety', 'insights / fixed-shape per-token decision receipt read bound'),
        'MAX_EVIDENCE_ENTRIES': (
            '16', 'safety', 'insights / fixed append-only evidence entries per insight'),
        'MAX_EVIDENCE_CHARS': (
            '8192', 'safety', 'insights / fixed evidence text bound per entry'),
    },
    'lock_diagnostics.py': {
        'MAX_LOCK_DIAGNOSTIC_ROWS': (
            '32', 'mechanism', 'lock diagnostics / fixed journal-line row bound'),
        'MAX_LOCK_DIAGNOSTIC_VALUE': (
            '2147483647', 'mechanism',
            'lock diagnostics / fixed signed-32-bit duration saturation'),
    },
    'runtime.py': {
        'MAX_ENGINE_TOOL_INVOCATIONS': (
            '4096', 'safety', 'runtime / fixed run-local engine-tool admission identity memory bound'),
        'MAX_ENGINE_TOOL_ID': (
            '256', 'mechanism', 'runtime / engine-tool invocation id format bound'),
        'OBSERVED_CURSOR_MAX_BYTES': (
            '4096', 'safety', 'runtime / bounded role cursor state read'),
        'MAX_COVERED_BYTES': (
            '2048', 'safety', 'runtime / bounded serialized CI-request covered-item set'),
        '_DECISION_IDLE_CACHE_LIMIT': (
            '256', 'safety', 'runtime / bounded in-process empty-decision cache entries'),
    },
    'work_items.py': {
        'MAX_REVISIONS': (
            '256', 'mechanism',
            'work items / revision-chain format bound including the immutable seed'),
        'MAX_OPEN_PAGE_ROWS': (
            '100', 'mechanism', 'work items / public open-page row contract'),
        'MAX_OPEN_CURSOR_CHARS': (
            '128', 'mechanism', 'work items / opaque cursor format bound'),
        'MAX_OPEN_ROW_BYTES': (
            '8192', 'safety', 'work items / bounded one-row response projection'),
        'MAX_OPEN_PAGE_BYTES': (
            '1024 * 1024', 'safety', 'work items / bounded page response'),
        'MAX_CLOSED_PAGE_ROWS': (
            '100', 'mechanism', 'work items / public closed-page row contract'),
    },
    'work_order.py': {
        'MAX_WORK_ORDER_ITEMS': (
            '100', 'mechanism',
            'work order / ordered IDs capped by the fixed 100-row work-item page contract'),
        'MAX_RATIONALE_CHARS': (
            '1024', 'safety',
            'work order / bounded operator-readable rationale text; record also byte-capped'),
        'MAX_WORK_ORDER_BYTES': (
            '8192', 'safety',
            'work order / bounded canonical current-record read, write and CAS envelope'),
    },
    'smoke.py': {
        'SMOKE_REQUESTS_PER_RUN': (
            '4', 'safety',
            'live smoke / fixed paid-probe request cap clamped below configured limits (ADR-006/ADR-013)'),
        'SMOKE_TOOLS_PER_RUN': (
            '5', 'safety',
            'live smoke / fixed paid-probe tool cap clamped below configured limits (ADR-006/ADR-013)'),
        'SMOKE_RUN_SECONDS': (
            '120', 'safety',
            'live smoke / fixed paid-probe wall cap clamped below configured limits (ADR-006/ADR-013)'),
    },
    'probes.py': {
        'MAX_SECONDS': (
            '90', 'safety',
            'recovery probes / fixed execution ceiling; configured budget and smaller limits still win'),
        'MAX_REQUESTS': (
            '2', 'safety',
            'recovery probes / fixed execution ceiling; real sponsor budget still admits'),
        'MAX_TOOLS': (
            '1', 'safety',
            'recovery probes / fixed execution ceiling; configured tool limit still wins'),
    },
    'selection.py': {
        'MAX_BYTES': (
            '1024 * 1024', 'safety',
            'selection / bounded configuration, input and shared-state file read'),
        'MAX_ITEMS': (
            '64', 'safety',
            'selection / fixed per-list entry cap that keeps validation and loops bounded'),
        'MAX_STATE_ITEMS': (
            '8192', 'safety',
            'selection / per-table stored-state entry cap'),
        'MAX_TERMINAL_CONTRIBUTIONS': (
            '256', 'configured policy',
            'selection / data-dir policy default for terminal probe-evidence retention'),
        'MAX_PROBE_EVENTS': (
            '8', 'configured policy',
            'selection / data-dir policy default for per-contribution probe-event retention'),
        'SELECTION_POLICY_BYTES': (
            '1024', 'safety', 'selection / bounded data-dir retention policy read'),
        'DEFAULT_ON_ERROR_RETRIES': (
            '2', 'configured policy',
            'selection / per-rule on_error retry default; rule field retries 0..64, '
            'documented in docs/model-selection.md'),
        'INTENT_RESERVE_BYTES': (
            '512', 'safety',
            'selection / bounded intent-event reservation in the saved probe ledger'),
        'INTENT_TERMINAL_RESERVE_BYTES': (
            '128', 'safety',
            'selection / minimum terminal-evidence reservation within an intent event'),
        'MAX_ON_ERROR_RETRIES': (
            '64', 'mechanism',
            'selection / fixed per-rule retry ceiling; no unbounded fallback loop'),
        'MAX_WORK_BYTES': (
            '131072', 'safety',
            'selection / bounded assembled classifier work-input document'),
    },
    'continuations.py': {
        'MAX_PENDING': (
            '64', 'safety',
            'writer continuations / bounded registry rows fitting the fixed 64 KiB pointer read'),
        'MAX_PER_ROLE': (
            '2', 'mechanism',
            'writer continuations / one reserved root plus its immediate child chain invariant'),
        'POINTER_BYTES': (
            '64 * 1024', 'safety',
            'writer continuations / bounded publication-pointer read'),
        'RUN_BYTES': (
            '1024 * 1024', 'safety',
            'writer continuations / bounded source run-envelope read'),
    },
    'startup.py': {
        'ADAPTER_STARTUP_FLOOR_SECONDS': (
            '60', 'mechanism',
            'adapter startup / floor of the published host-relative bound (ADR-042)'),
        'ADAPTER_STARTUP_MULTIPLIER': (
            '12', 'mechanism',
            'adapter startup / published host-relative multiplier (ADR-042)'),
        'ADAPTER_STARTUP_CEILING_SECONDS': (
            '150', 'mechanism',
            'adapter startup / ceiling of the published host-relative bound (ADR-042)'),
        'ADAPTER_STARTUP_MEASURE_CEILING_SECONDS': (
            '150', 'safety',
            'adapter startup / one-shot probe measurement cap; timeout keeps the floor'),
        'ADAPTER_PROBE_MAXIMUM_BYTES': (
            '8192', 'safety',
            'adapter startup / bounded combined probe stdout/stderr capture'),
        'ADAPTER_CODEX_PROBE_MAXIMUM_BYTES': (
            '262144', 'safety',
            'adapter startup / bounded Codex help-probe text capture'),
        'RECORD_MAX_BYTES': (
            '8192', 'safety',
            'adapter startup / bounded host measurement-record read'),
    },
    'vcs.py': {
        'REF_CONTENT_MAX': (
            '4096', 'safety', 'vcs ref store / bounded local ref-content read'),
        'MAX_ARTIFACT_FIELD_NAMES': (
            '8', 'safety',
            'VCS artifact shape refusal / bounded field identifiers named; values are never echoed'),
        'MAX_ARTIFACT_FIELD_NAME_CHARS': (
            '32', 'safety',
            'VCS artifact shape refusal / bounded disclosed identifier length'),
        'MAX_REFS': ('4096', 'mechanism', 'VCS refs / adapter-shape fetch and injection bound'),
        'MAX_ACQUIRE_CONTENT': (
            '524288', 'safety', 'VCS acquire / bounded acquired content; callers narrow scope'),
        'MAX_ARTIFACTS': ('256', 'mechanism', 'VCS artifacts / bounded true artifact entries'),
        'MAX_ARTIFACT_NAME': (
            '512', 'mechanism', 'VCS artifacts / bounded relative file-name chars'),
        'MAX_ARTIFACT_CONTENT': (
            '524288', 'safety', 'VCS artifacts / bounded whole-artifact text chars'),
        'MAX_ARTIFACT_PAGE_BYTES': (
            '8192', 'mechanism', 'VCS artifacts / bounded UTF-8 page read'),
        'MAX_CHECKS': ('1024', 'mechanism', 'VCS status / adapter payload already capped'),
        'MAX_PROPOSALS': ('256', 'mechanism', 'VCS proposals / bounded external proposal rows'),
        'MAX_REVIEWS': ('64', 'mechanism', 'VCS proposals / bounded independent reviews per row'),
        'MAX_PROPOSAL_CHECK_DETAIL_BYTES': (
            '16 * 1024', 'safety', 'VCS proposals / fixed UTF-8 budget for retained check detail'),
        'MAX_CLOSE_IF_ADOPTED_REASON': (
            '4096', 'mechanism', 'VCS conditional close / bounded reason text'),
        'MAX_CLOSE_IF_ADOPTED_MISMATCHES': (
            '50', 'mechanism', 'VCS conditional close / bounded mismatch rows'),
        'MAX_CLOSE_IF_ADOPTED_PATH': (
            '4096', 'mechanism', 'VCS conditional close / bounded path text'),
        'MAX_CLOSE_IF_ADOPTED_CHANGED_PATHS': (
            '2000', 'mechanism', 'VCS conditional close / bounded reported changed-path count'),
        'MAX_CI_WORKFLOWS': ('8', 'mechanism', 'VCS CI request / at most eight workflows'),
        'MAX_CI_JOB_SECONDS': (
            '7 * 86400', 'safety',
            'VCS CI status / one-week sanity ceiling for reported job time'),
    },
    'vcs_poll_policy.py': {
        'MAX_CI_BRANCHES': ('4', 'configured policy', 'VCS polling / fail-closed fallback for poll_max_branches'),
        'POLL_CALL_TIMEOUT_CAP': ('15', 'configured policy', 'VCS polling / fail-closed fallback for poll_status_timeout_seconds'),
        'POLL_FETCH_TIMEOUT_CAP': ('30', 'configured policy', 'VCS polling / fail-closed fallback for poll_fetch_timeout_seconds'),
    },
    'smoke_retention.py': {
        'MAX_RECORD': ('128 * 1024', 'safety', 'smoke retention / bounded metadata-record read'),
        'MAX_DAYS': ('36500', 'safety', 'smoke retention / stored expiry-age validation maximum (100 years)'),
        'AUDIT_LIMIT': (
            '128', 'safety',
            'smoke retention / bounded maintenance-audit record read and retained event count'),
        '_AUTOMATIC_SMOKE_RETENTION_STATE_BYTES': (
            '4096', 'safety', 'smoke retention / bounded automatic due-state read'),
        'AUTOMATIC_SMOKE_RETENTION_INTERVAL_MAX': (
            '2678400', 'safety',
            'configuration validation / maximum accepted automatic smoke-retention interval (31 days)'),
    },
    'check_jobs.py': {
        'PURPOSE_MAX': (
            '64', 'format',
            'check jobs / stored purpose label character bound'),
    },
    'delegation_gates.py': {
        'POLICY_MAX_BYTES': (
            '65536', 'safety',
            'delegation gates / bounded policy-document read'),
    },
    'distribution.py': {
        'PUBLIC_SOURCE_ALGORITHM_CHARS': (
            '128', 'format',
            'distribution / public-source algorithm label bound'),
    },
    'editor.py': {
        'BUNDLE_MANIFEST_BYTES': (
            '16 * 1024 * 1024', 'safety',
            'editor bundle / bounded manifest read'),
        'BUNDLE_CHANGES_BYTES': (
            '262144', 'safety',
            'editor bundle / bounded changed-text read'),
    },
    'engine_config.py': {
        'MAX_SESSION_TOKENS': (
            '2 ** 63 - 1', 'format',
            'engine config / signed 64-bit session-token range'),
    },
    'explain.py': {
        'RECORD_BYTES': (
            '65536', 'safety',
            'explain / bounded evidence-record read'),
    },
    'host_records.py': {
        'SCHEMA_ENTRIES': (
            '16', 'format',
            'host records / fixed schema field count'),
    },
    'insight_tools.py': {
        'INSIGHT_RETRIEVAL_LIMIT': (
            '1000', 'mechanism',
            'insight retrieval / fixed public page bound'),
    },
    'lineage.py': {
        'TARGET_VERSION_MAX': (
            '1073741824', 'format',
            'lineage / target version integer bound'),
        'TARGET_BRANCH_MAX': (
            '256', 'format',
            'lineage / target branch name character bound'),
        'TARGET_REMOTE_MAX': (
            '256', 'format',
            'lineage / target remote name character bound'),
    },
    'materialize.py': {
        'SPEC_MAX': (
            '8192', 'safety',
            'materialize / bounded caller spec text'),
        'CACHE_ID_MAX': (
            '256', 'format',
            'materialize / cache identity character bound'),
        '_STARTED_MAX': (
            '65536', 'safety',
            'materialize / bounded started-marker read'),
        '_OUTPUT_MAX': (
            '4096', 'safety',
            'materialize / bounded adapter output read'),
    },
    'model_catalog.py': {
        'OBSERVATION_BYTES': (
            '64 * 1024', 'safety',
            'model catalog / bounded observation text'),
        'OBSERVATION_COUNT': (
            '128', 'safety',
            'model catalog / bounded observation rows'),
    },
    'pi_durable.py': {
        'RESULT_BYTES': (
            '1024 * 1024', 'safety',
            'pi-durable / bounded result read'),
        'MAX_ATTEMPT_ORDINAL': (
            '64 * 64', 'format',
            'pi-durable / attempt ordinal schema ceiling'),
        'COMPACTION_TOKEN_MAX': (
            '1073741824', 'format',
            'pi-durable / compaction token integer ceiling'),
    },
    'pi_durable_store.py': {
        'MAX_PAYLOAD_BYTES': (
            '262144', 'safety',
            'pi-durable store / bounded payload column'),
    },
    'process.py': {
        'ADAPTER_DIAGNOSTIC_BYTES_MAX': (
            '8192', 'safety',
            'process / bounded adapter diagnostic text'),
        'STOP_IO_TIMEOUT_SECONDS': (
            '5.0', 'safety',
            'process / bounded subprocess I/O stop wait'),
    },
    'report.py': {
        'PREVIOUS_REPORT_BYTES': (
            '8192', 'safety',
            'report / bounded previous-report read'),
        'PREVIOUS_REPORT_READ_BYTES': (
            '262144', 'safety',
            'report / bounded previous-report evidence read'),
    },
    'role_state.py': {
        'MAX_DEPTH': (
            '4', 'format',
            'role state / fixed nested-object depth'),
        'MAX_KEY_CHARS': (
            '64', 'format',
            'role state / key character bound'),
        'MAX_RUN_CHARS': (
            '128', 'format',
            'role state / run identity character bound'),
        'MAX_RECORD_BYTES': (
            '65536', 'safety',
            'role state / bounded record read'),
    },
    'routing.py': {
        'MAX_PROFILE_CHARS': (
            '63', 'format',
            'routing / profile label character bound'),
        'MAX_REASON_CHARS': (
            '256', 'format',
            'routing / reason text character bound'),
        'MAX_RECORD_BYTES': (
            '1024', 'safety',
            'routing / bounded routing record'),
    },
    'services.py': {
        'WINDOWS_RESTART_COUNT': (
            '999', 'mechanism',
            'service rendering / Windows restart-count schema maximum'),
    },
    'shared_records.py': {
        'RECORD_BYTES': (
            '1024 * 1024 + 4096', 'safety',
            'shared records / bounded canonical envelope'),
        'RECEIPT_BYTES': (
            '2048', 'safety',
            'shared records / bounded receipt read'),
        'MANIFEST_ROWS': (
            '512', 'mechanism',
            'shared records / fixed batch row bound'),
    },
    'smoke_refs.py': {
        'MAX_SMOKE_REFS': (
            '64', 'mechanism',
            'smoke refs / fixed per-item reference count'),
    },
    'snapshot.py': {
        '_PATH_VALIDATION_CACHE_LIMIT': (
            '256', 'safety',
            'snapshot / bounded in-process path-validation cache'),
        'MAX_MISSING_FINISH_STREAK': (
            '1073741824', 'format',
            'snapshot / persisted missing-finish streak counter ceiling'),
    },
    'storage_runs.py': {
        'MAX_ENTRIES': (
            '100000', 'safety',
            'storage runs / bounded scan entry ceiling'),
        'MAX_BYTES': (
            '64 * 1024 * 1024', 'safety',
            'storage runs / bounded aggregate scan bytes'),
        'RECORD_BYTES': (
            '1024 * 1024', 'safety',
            'storage runs / bounded record read'),
        'PREVIEW_ROWS': (
            '100', 'mechanism',
            'storage runs / fixed preview row count'),
        'MAX_DEPTH': (
            '8', 'mechanism',
            'storage runs / fixed traversal depth'),
    },
    'unit_prompts.py': {
        'MAX_READY_WORK_AGE_SECONDS': (
            '2 ** 31 - 1', 'format',
            'unit prompts / age render clamp'),
    },
    'vcs_proposal_poll.py': {
        'MAX_PROPOSAL_CURSOR': (
            '256', 'format',
            'proposal polling / cursor character bound'),
    },
    'wait_events.py': {
        'MAX_ROW_BYTES': (
            '1024 * 1024', 'safety',
            'wait events / bounded row read'),
        'RECEIPT_BYTES': (
            '4096', 'safety',
            'wait events / bounded receipt read'),
    },
    'work_item_seed.py': {
        'MAX_DECISION_TASKS': (
            '16', 'format',
            'work-item seed / fixed task count'),
    },
    'workspace_reads.py': {
        'MAX_READ_IDENTITIES': (
            '1024', 'mechanism',
            'workspace reads / fixed identity page bound'),
    },
}


# The validation gate is a separate owner. Its enforced host-cost choices have
# configuration owners: the suite child, shared phase budget, per-check ceiling
# and provisioning bound each have a validated option. The two modelled-path
# reserves are fixed conservative safety margins, not command bounds, and
# output/artifact bounds are fixed safety ceilings.
GATE_DECLARATIONS = {
    'check_unittest.py': {
        'TIMING_SIDECAR_MAX_BYTES': (
            '1000000', 'safety', 'gate timing artifact / bounded sidecar read'),
        'TIMING_MAX_SECONDS': (
            '100000.0', 'safety', 'gate timing artifact / accepted timestamp range'),
    },
    'check.py': {
        'DEFAULT_CHECK_TIMEOUT_SECONDS': (
            '600', 'configured policy',
            'gate later phase / per-check ceiling default; --check-timeout'),
        'GATE_TIMEOUT_MIN_SECONDS': (
            '1', 'safety', 'gate configuration / minimum accepted host-cost bound'),
        'GATE_TIMEOUT_MAX_SECONDS': (
            '86400', 'safety', 'gate configuration / maximum accepted host-cost bound'),
        'PROCESS_CLEANUP_TIMEOUT_SECONDS': (
            '30', 'mechanism', 'gate / bounded process-group cleanup wait'),
        'SETUP_RESERVE_SECONDS': (
            '60', 'safety',
            'gate modelled path / fixed conservative setup margin; not an enforced command bound'),
        'RECEIPT_RESERVE_SECONDS': (
            '120', 'safety',
            'gate modelled path / fixed conservative receipt/summary margin; not an enforced command bound'),
        'LATER_PHASE_BUDGET_SECONDS': (
            '1200', 'configured policy', 'gate later phase / default shared budget; --later-phase-budget'),
        'RELAY_BUDGET_BYTES': (
            '49152', 'safety', 'gate output relay / bounded retained child output'),
        'RELAY_TAIL_BYTES': (
            '16384', 'safety', 'gate output relay / bounded failure tail'),
        'RECEIPT_OUTPUT_BUDGET_BYTES': (
            '16384', 'safety', 'gate output relay / bounded receipt output'),
        'SUMMARY_OUTPUT_BUDGET_BYTES': (
            '8192', 'safety', 'gate output relay / bounded summary output'),
        'PROGRESS_OUTPUT_BUDGET_BYTES': (
            '98304', 'safety', 'gate output relay / bounded live progress output'),
        'FINAL_NOTICE_BUDGET_BYTES': (
            '1024', 'safety', 'gate output relay / bounded final notice'),
        'SUITE_CHILD_TIMEOUT_SECONDS': (
            '1800', 'configured policy', 'gate suite child / default wall bound; --suite-child-timeout'),
        'TIMING_SIDECAR_MAX_BYTES': (
            '1000000', 'safety', 'gate timing artifact / bounded sidecar read'),
        'TIMING_MAX_SECONDS': (
            '100000.0', 'safety', 'gate timing artifact / accepted timestamp range'),
        'TIMING_PHASE_MAX_ENTRIES': (
            '200', 'safety', 'gate timing artifact / bounded phase entries'),
        'TIMING_COUNTS_MAX': (
            '1000000', 'safety', 'gate timing artifact / accepted event-count range'),
        'TIMING_ARTIFACT_MAX_BYTES': (
            '192 * 1024', 'safety', 'gate timing artifact / bounded artifact page'),
        'NPM_PROVISION_TIMEOUT_SECONDS': (
            '300', 'configured policy',
            'gate provisioning / pinned adapter install timeout default; --provision-timeout'),
        'PROJECT_IMPORT_DIAGNOSTIC_BYTES': (
            '8192', 'safety', 'gate import diagnostic / bounded sidecar read'),
    },
}


# Migrated declarations live in the owning config dataclass or the explicit
# raw-settings default table checked below. This is not a full config inventory.
CONFIGURED_SETTINGS = {
    'web': {
        'search_feed_item_limit': ('200', 'configured policy', 'web / per-feed RSS/Atom candidate shaping'),
    },
    'vcs': {
        'poll_max_branches': ('4', 'configured policy', 'VCS polling / branches considered per daemon tick'),
        'poll_fetch_timeout_seconds': ('30', 'configured policy', 'VCS polling / upstream fetch timeout default'),
        'poll_status_timeout_seconds': ('15', 'configured policy', 'VCS polling / per-branch status timeout default'),
        'ci_rewake_seconds': ('1800', 'configured policy',
                              'VCS CI wake / unchanged-red re-wake interval default'),
    },
}
CONFIGURED = {
    'Usage': {
        'runs_scanned': ('5000', 'configured policy', 'usage / history scan work budget'),
        'finish_rate_runs_scanned': ('512', 'configured policy', 'usage / finish-only status history window'),
        'recent_entries': ('200', 'configured policy', 'usage / recent-entry output sample'),
        'unknown_keys': ('20', 'configured policy', 'usage / unknown-key diagnostic sample'),
        'cache_affinity_rows': ('200', 'configured policy', 'usage / affinity output sample'),
        'cache_affinity_runs': ('200', 'configured policy', 'usage / affinity history window'),
    },
    'CiRequestEvidence': {
        'name_processing_limit': ('10000', 'configured policy', 'CI requests / name-processing budget after os.listdir materializes all names'),
        'record_limit': ('1000', 'configured policy', 'CI requests / candidate-record validation budget'),
    },
    'ProjectImport': {
        'status_timeout_seconds': (
            '30', 'configured policy', 'project import / git status command wall-time budget'),
        'clone_timeout_seconds': (
            '300', 'configured policy', 'project import / git clone command wall-time budget'),
        'local_timeout_seconds': (
            '15', 'configured policy', 'project import / local git config and remote command budget'),
    },
    'PublicationPolicy': {
        'source_commit_timeout_seconds': ('5', 'configured policy', 'publication / local Git HEAD command wall-time budget'),
    },
    'PublicationEvidence': {
        'name_processing_limit': ('10000', 'configured policy', 'publication / name-processing budget after os.listdir materializes all names'),
        'record_limit': ('1000', 'configured policy', 'publication / candidate-record validation budget'),
    },
    'SchedulerEvidence': {
        'candidate_entries': ('5000', 'configured policy', 'scheduler evidence / diagnostic enumeration budget'),
        'files': ('200', 'configured policy', 'scheduler evidence / diagnostic read window'),
        'scan_seconds': ('2', 'configured policy', 'scheduler evidence / cooperative diagnostic deadline'),
    },
    'Doctor': {
        'runtime_version_seconds': (
            '20', 'configured policy', 'doctor / container-runtime version probe wall deadline'),
        'namespace_helper_seconds': (
            '10', 'configured policy', 'doctor / namespace-helper readiness probe wall deadline'),
        'image_inspect_seconds': (
            '15', 'configured policy', 'doctor / image-presence inspect wall deadline'),
        'node_capability_seconds': (
            '10', 'configured policy', 'doctor / Node capability probe wall deadline'),
        'sandbox_smoke_seconds': (
            '30', 'configured policy', 'doctor / sandbox smoke command wall deadline'),
    },
    'WorkspaceCache': {
        'roots': ('()', 'configured policy', 'workspace cache / opt-in regenerable roots'),
        'max_bytes': ('1073741824', 'configured policy', 'workspace cache / aggregate removal threshold'),
        'check_interval_seconds': ('3600', 'configured policy', 'workspace cache / sweep cadence'),
        'max_entries': ('100000', 'configured policy', 'workspace cache / aggregate metadata entry budget'),
        'max_depth': ('MAX_WORKSPACE_CACHE_DEPTH', 'configured policy', 'workspace cache / per-root traversal depth under safety ceiling'),
        'scan_seconds': ('10', 'configured policy', 'workspace cache / scan time after index lookup'),
        'git_timeout_seconds': ('10', 'configured policy', 'workspace cache / per-command Git timeout'),
        'git_output_bytes': ('8388608', 'configured policy', 'workspace cache / index listing output budget'),
    },
}


# The Limits migration is field-scoped, not an inventory of every Limits field.
CHECK_RETENTION = {
    'check_retained': ('1024', 'configured policy', 'Limits / per-project retained check records including subscriptions'),
    'MAX_CHECK_RETAINED': ('1024', 'safety', 'config / existing defensive linear metadata-scan and identity-search envelope'),
}


# The active recovery budget is policy; its separate absolute ceiling is
# name-scoped, not a WorkItems numeric inventory.
HISTORY_TEMPORARIES = {
    'history_temporaries': ('DEFAULT_HISTORY_TEMPORARIES', 'configured policy',
                            'Limits / per-project decision-history recovery work budget'),
    'DEFAULT_HISTORY_TEMPORARIES': ('64', 'configured policy',
                                    'config / unchanged decision-history recovery default'),
    'MAX_HISTORY_TEMPORARIES': ('4096', 'safety',
                                'config / absolute untrusted temporary-metadata ceiling'),
}


# The shared scan traverses the machine-wide data dir. A bounded synthetic
# measurement found ~0.6 ms per project root and per metadata record and
# ~4.8 KiB retained per insight, so these dimensions are host/history policy.
SMOKE_RETENTION_SCAN = {
    'smoke_retention_projects': (
        'DEFAULT_SMOKE_RETENTION_PROJECTS', 'configured policy',
        'Limits / shared scan project roots'),
    'smoke_retention_pairs': (
        'DEFAULT_SMOKE_RETENTION_PAIRS', 'configured policy',
        'Limits / shared scan pairs and quarantine entries'),
    'smoke_retention_refs': (
        'DEFAULT_SMOKE_RETENTION_REFS', 'configured policy',
        'Limits / shared scan live reference occurrences'),
    'smoke_retention_entries': (
        'DEFAULT_SMOKE_RETENTION_ENTRIES', 'configured policy',
        'Limits / shared scan metadata entries plus record reads'),
    'smoke_retention_metadata_bytes': (
        'DEFAULT_SMOKE_RETENTION_METADATA_BYTES', 'configured policy',
        'Limits / shared scan cumulative metadata bytes'),
    'smoke_retention_report_bytes': (
        'DEFAULT_SMOKE_RETENTION_REPORT_BYTES', 'configured policy',
        'Limits / shared scan cumulative report payload bytes'),
    'DEFAULT_SMOKE_RETENTION_PROJECTS': (
        '5000', 'configured policy', 'config / shared scan root default'),
    'DEFAULT_SMOKE_RETENTION_PAIRS': (
        '5000', 'configured policy', 'config / shared scan pair default'),
    'DEFAULT_SMOKE_RETENTION_REFS': (
        '20000', 'configured policy', 'config / shared scan reference default'),
    'DEFAULT_SMOKE_RETENTION_ENTRIES': (
        '100000', 'configured policy', 'config / shared scan entry default'),
    'DEFAULT_SMOKE_RETENTION_METADATA_BYTES': (
        '64 * 1024 * 1024', 'configured policy', 'config / shared scan byte default'),
    'DEFAULT_SMOKE_RETENTION_REPORT_BYTES': (
        '5000 * 16 * 1024 * 1024', 'configured policy',
        'config / shared scan report-byte default'),
}


# The receipt-diagnostic and queue-observation samples are operator-selected
# output/evidence policy. Their ceilings keep one page or run record bounded;
# neither is a history-sized scan or a delivery bound.
DIAGNOSTIC_SAMPLES = {
    'decision_receipt_diagnostics': (
        'DEFAULT_DECISION_RECEIPT_DIAGNOSTICS', 'configured policy',
        'Limits / malformed-receipt diagnostics per decision page'),
    'DEFAULT_DECISION_RECEIPT_DIAGNOSTICS': (
        '10', 'configured policy', 'config / unchanged receipt-diagnostic sample default'),
    'MAX_DECISION_RECEIPT_DIAGNOSTICS': (
        '100', 'safety', 'config / fixed companion output-page diagnostic ceiling'),
    'queue_observations': (
        'DEFAULT_QUEUE_OBSERVATIONS', 'configured policy',
        'Limits / contended-admission observations per started.json'),
    'DEFAULT_QUEUE_OBSERVATIONS': (
        '8', 'configured policy', 'config / unchanged queue-observation sample default'),
    'MAX_QUEUE_OBSERVATIONS': (
        '100', 'safety',
        'config / count ceiling; runtime also fits the sample to the 64 KiB started.json read cap'),
}


# The current-decision projection is history policy; its separate absolute
# in-process row ceiling is name-scoped, not a full config.py inventory.
DECISION_PROJECTION = {
    'decision_projection_records': ('DEFAULT_DECISION_PROJECTION_RECORDS', 'configured policy',
                                    'Limits / combined decision and pending-attention page retention rows'),
    'DEFAULT_DECISION_PROJECTION_RECORDS': ('512', 'configured policy',
                                            'config / single page-retention default, independent of the measured ceiling'),
    'MAX_DECISION_PROJECTION_RECORDS': ('512', 'safety',
                                        'config / measured in-process projection-row ceiling'),
}


# The host-only activation plan/report is history-sized local evidence. Its
# default is configured policy; the ceiling bounds activation-time parsing and
# the retained report's exact-byte check, not a transport frame.
DECISION_RECONCILIATION = {
    'decision_reconciliation_bytes': (
        'DEFAULT_DECISION_RECONCILIATION_BYTES', 'configured policy',
        'Limits / serialized activation plan and post-map report bytes'),
    'DEFAULT_DECISION_RECONCILIATION_BYTES': (
        '8 * 1024 * 1024', 'configured policy',
        'config / activation plan/report default for several thousand routed rows'),
    'MAX_DECISION_RECONCILIATION_BYTES': (
        '64 * 1024 * 1024', 'safety',
        'config / bounded activation-time plan/report parse and retained report digest read; '
        'runtime parses only the 4096-byte marker'),
}


# Remaining config.py module-level candidates not owned by the special
# policy/default dictionaries above. These are fixed mechanism or safety
# ceilings; MAX_WAIT_SECONDS is the protocol format bound. The inventory test
# adds the module-level names already declared by those dictionaries, so a
# new unexplained config.py literal fails it.
CONFIG_MODULE_BOUNDS = {
    'MAX_RESOURCES': (
        '64', 'safety', 'config / bounded per-profile local resource list'),
    'MAX_CI_WAKE_EXTRA_BRANCHES': (
        '64', 'mechanism', 'config / fixed CI wake branch-set bound'),
    'MAX_WAIT_SECONDS': (
        '86400', 'format', 'protocol / finish wait_seconds wire bound mirrored by config ranges'),
    'MAX_INCLUDE_DEPTH': (
        '8', 'mechanism', 'config include / fixed graph depth'),
    'MAX_INCLUDE_FILES': (
        '32', 'mechanism', 'config include / fixed file-count bound'),
    'MAX_INCLUDE_BYTES': (
        '1048576', 'mechanism', 'config include / fixed cumulative TOML byte budget'),
    'MAX_INCLUDE_ENTRIES': (
        '32', 'mechanism', 'config include / fixed entries per included file'),
    'MAX_ROLE_POLICY_PARTS': (
        '16', 'mechanism', 'config role policy / fixed Markdown part count'),
    'MAX_ROLE_POLICY_BYTES': (
        '65536', 'mechanism', 'config role policy / fixed bytes per Markdown part'),
    'MAX_ROLE_POLICY_TOTAL': (
        '262144', 'mechanism', 'config role policy / fixed composed-text byte total'),
    'TOML_INTEGER_MAX': (
        '(1 << 63) - 1', 'format', 'config / TOML signed-64-bit integer wire range'),
    'EVENT_POLL_SECONDS_MAX': (
        '31 * 86400', 'safety', 'config / event-poll interval validation maximum (31 days)'),
    'MAX_WORKSPACE_CACHE_DEPTH': (
        '64', 'safety', 'workspace cache / traversal depth ceiling; configured max_depth may lower it'),
    'DOCTOR_TIMEOUT_MAX_SECONDS': (
        '86400', 'safety', 'doctor / probe deadline safety ceiling; configured values may lower it'),
}


CALL_KEYWORDS = frozenset({
    'timeout', 'timeout_seconds', 'maximum', 'max_bytes', 'max_files',
    'max_entries', 'max_depth', 'limit',
})
# (enclosing scope, callee, keyword, occurrence): (expression, class, rationale)
CALLS = {
    'usage.py': {},
    'dispatch_evidence.py': {},
    'workspace_cache.py': {
        ('_preflight', 'run', 'maximum', 0): (
            '4096', 'safety', 'workspace cache / Git root output read bound'),
        ('_due', 'read_json', 'max_bytes', 0): (
            '4096', 'safety', 'workspace cache / bounded automatic due-state read'),
    },
    'ci_requests.py': {},
    'insights.py': {},
    'runtime.py': {
        ('_has_ready_work', 'project.work_items.page_ready', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row readiness existence check'),
        ('_ready_work_changed', 'store.page_ready', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row readiness fingerprint check'),
        ('Context._op_vcs_read_publications', 'Publication.list_receipts', 'limit', 0): (
            '100', 'mechanism', 'runtime / publication receipt public page bound'),
        ('Context._op_vcs_read_ci_requests',
         'CiRequests(self.project.root, self.config.ci_requests).daily', 'limit', 0): (
            '100', 'mechanism', 'runtime / CI-request public page bound'),
        ('Engine._dispatch_state', 'decision_delivery.pending', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row pending-decision dispatch check'),
        ('Engine._run', 'read_json', 'max_bytes', 0): (
            '1024 * 1024', 'safety', 'runtime / bounded run-state record read'),
        ('Engine._run', 'project.work_items.page_ready', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row readiness check during a run'),
        ('_role_events_due', 'work_item_events.pending', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row work-item event check'),
        ('_role_events_due', 'decision_delivery.pending', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row decision-delivery check'),
        ('_role_events_due', 'ci_waits.pending', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row CI-wait check'),
        ('_role_events_due', 'proposal_waits.pending', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row proposal-wait check'),
        ('should_run', 'ci_waits.pending', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row CI-wait scheduling check'),
        ('should_run', 'proposal_waits.pending', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row proposal-wait scheduling check'),
    },
    'project.py': {
        ('config_managed_repo', 'run', 'maximum', 0): (
            '8192', 'safety', 'project import / local git config output read bound'),
        ('_import_git_tree', 'run', 'maximum', 0): (
            '65536', 'safety', 'project import / git status output read bound'),
        ('_import_git_tree', 'run', 'maximum', 1): (
            '1048576', 'safety', 'project import / git clone output read bound'),
        ('_import_git_tree', 'run', 'maximum', 2): (
            '8192', 'safety', 'project import / origin removal output read bound'),
    },
    'publication.py': {
        ('source_commit', 'process.run', 'maximum', 0): (
            '4096', 'safety', 'publication / local Git HEAD output read bound'),
    },
    'smoke_retention.py': {
        ('collect_refs', 'ReferenceSnapshots', 'max_bytes', 0): (
            '2 ** 63 - 1', 'mechanism',
            'smoke retention / disable duplicate snapshot byte cap; outer metadata reader is bounded'),
    },
    'bridge.py': {
        ('Bridge.__exit__', 'self.thread.join', 'timeout', 0): (
            '2', 'safety', 'bridge / bounded listener-thread join during shutdown'),
    },
    'ci_waits.py': {
        ('_resolved', 'Publication.list_receipts', 'limit', 0): (
            '2', 'mechanism', 'CI wait / strict uniqueness check reads one extra receipt'),
    },
    'cli.py': {
        ('_cmd_insight', 'project.insights.list', 'limit', 0): (
            '1000', 'mechanism', 'CLI / fixed legacy insight-list page bound'),
    },
    'doctor.py': {
        ('container_runtime', 'run', 'maximum', 0): (
            '8192', 'safety', 'doctor / bounded runtime version output read'),
        ('container_runtime', 'run', 'maximum', 1): (
            '1024', 'safety', 'doctor / bounded namespace-helper output read'),
        ('node_capability', 'run', 'maximum', 0): (
            '1024', 'safety', 'doctor / bounded Node capability output read'),
    },
    'engine_channel.py': {
        ('Channel._put', 'self.records.put', 'timeout', 0): (
            '0.1', 'safety', 'engine channel / bounded handshake queue put'),
        ('Channel.receive', 'self.records.get', 'timeout', 0): (
            '0.1', 'safety', 'engine channel / bounded handshake queue get'),
        ('Channel.close', 'self.process.wait', 'timeout', 0): (
            '1', 'safety', 'engine channel / bounded child-exit wait during teardown'),
        ('Channel.close', 'thread.join', 'timeout', 0): (
            '0.5', 'safety', 'engine channel / bounded reader-thread join during teardown'),
    },
    'engine_config.py': {
        ('stop_engine_containers', 'run', 'timeout', 0): (
            '15', 'policy', 'engine config / host-dependent MCP container cleanup wall; '
            'policy pending measurement'),
        ('stop_engine_containers', 'run', 'maximum', 0): (
            '8192', 'safety', 'engine config / bounded cleanup output read'),
    },
    'operator_records.py': {
        ('store', 'KindPolicy', 'maximum', 0): (
            '140000', 'safety', 'operator records / bounded shared-record payload envelope'),
    },
    'persistent_exec.py': {
        ('ExecSession._remove', 'run', 'timeout', 0): (
            '15', 'policy', 'persistent exec / host-dependent launcher cleanup wall; '
            'policy pending measurement'),
        ('ExecSession._remove', 'run', 'maximum', 0): (
            '8192', 'safety', 'persistent exec / bounded cleanup output read'),
    },
    'pi_reselection.py': {
        ('_family_record', 'read_json', 'max_bytes', 0): (
            '4096', 'safety', 'pi reselection / bounded family record read'),
    },
    'platform.py': {
        ('terminate_process', 'process.wait', 'timeout', 0): (
            '2', 'safety', 'platform / bounded process reap wait'),
    },
    'proposal_waits.py': {
        ('shared_store', 'KindPolicy', 'maximum', 0): (
            '8192', 'safety', 'proposal waits / bounded shared-record payload envelope'),
    },
    'safe_point.py': {
        ('read', 'read_json', 'max_bytes', 0): (
            '4096', 'safety', 'safe point / bounded saved-state read'),
    },
    'sandbox.py': {
        ('assert_no_checkpoint', 'run', 'timeout', 0): (
            '15', 'policy', 'sandbox / host-dependent checkpoint-image inspect wall; '
            'policy pending measurement'),
        ('remove_leftover', 'run', 'timeout', 0): (
            '15', 'policy', 'sandbox / host-dependent leftover-container list wall; '
            'policy pending measurement'),
        ('remove_leftover', 'run', 'maximum', 0): (
            '8192', 'safety', 'sandbox / bounded leftover-container list output'),
        ('remove_leftover', 'run', 'timeout', 1): (
            '15', 'policy', 'sandbox / host-dependent leftover-container removal wall; '
            'policy pending measurement'),
        ('remove_leftover', 'run', 'maximum', 1): (
            '8192', 'safety', 'sandbox / bounded leftover-container removal output'),
        ('Sandbox.execute', 'run', 'timeout', 0): (
            '15', 'policy', 'sandbox / host-dependent post-command removal wall; '
            'policy pending measurement'),
        ('Sandbox.execute', 'run', 'maximum', 0): (
            '8192', 'safety', 'sandbox / bounded cleanup output read'),
        ('_remove_containers', 'run', 'timeout', 0): (
            '15', 'policy', 'sandbox / host-dependent project-container list wall; '
            'policy pending measurement'),
        ('_remove_containers', 'run', 'maximum', 0): (
            '65536', 'safety', 'sandbox / bounded project-container list output'),
        ('_remove_containers', 'run', 'timeout', 1): (
            '30', 'policy', 'sandbox / host-dependent bulk removal wall; '
            'policy pending measurement'),
        ('_remove_containers', 'run', 'maximum', 1): (
            '65536', 'safety', 'sandbox / bounded bulk removal output'),
    },
    'services.py': {
        ('install', 'subprocess.run', 'timeout', 0): (
            '60', 'policy', 'services / host-dependent systemd-analyze verification wall; '
            'policy pending measurement'),
    },
}


def numeric(node):
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float)
    if isinstance(node, ast.UnaryOp):
        return numeric(node.operand)
    if isinstance(node, ast.BinOp):
        return numeric(node.left) and numeric(node.right)
    return False


def inventory(tree, extras=()):
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if (isinstance(target, ast.Name)
                    and (target.id in extras or CANDIDATE.fullmatch(target.id))
                    and numeric(value)):
                if target.id in found:
                    raise AssertionError(f'Duplicate bound declaration: {target.id}')
                found[target.id] = ast.unparse(value)
    return found


def callee_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f'{callee_name(node.value)}.{node.attr}'
    return ast.unparse(node)


def call_inventory(tree):
    found, occurrences = {}, {}

    class Calls(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Call(self, node):
            callee = callee_name(node.func)
            for keyword in node.keywords:
                if keyword.arg not in CALL_KEYWORDS or not numeric(keyword.value):
                    continue
                base = ('.'.join(self.scope) or '<module>', callee, keyword.arg)
                index = occurrences.get(base, 0)
                occurrences[base] = index + 1
                found[(*base, index)] = ast.unparse(keyword.value)
            self.generic_visit(node)

    Calls().visit(tree)
    return found


class RuntimeBoundDeclarationTests(unittest.TestCase):
    def test_declared_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for filename, declarations in DECLARATIONS.items():
            with self.subTest(file=filename):
                for expression, category, rationale in declarations.values():
                    self.assertIn(category, categories)
                    self.assertTrue(rationale.strip())
                tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
                self.assertEqual(inventory(tree, EXTRA_CANDIDATES.get(filename, ())), {
                    name: entry[0] for name, entry in declarations.items()
                })

    def test_every_module_with_bounds_is_inventoried(self):
        # config.py has its own field-scoped inventory; scripts/check.py is the
        # separate gate inventory. Every other module must be in DECLARATIONS.
        covered = set(DECLARATIONS) | {'config.py'}
        missing = {}
        for path in sorted(ROOT.glob('*.py')):
            if path.name in covered:
                continue
            found = inventory(ast.parse(path.read_text(encoding='utf-8')))
            if found:
                missing[path.name] = sorted(found)
        self.assertEqual(missing, {}, 'Undeclared module bounds: %r' % (missing,))

    def test_policy_defaults_are_declared(self):
        for filename, declarations in POLICY_DEFAULTS.items():
            with self.subTest(file=filename):
                tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
                found = {}
                for node in tree.body:
                    if not isinstance(node, ast.Assign):
                        continue
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in declarations:
                            found[target.id] = ast.literal_eval(node.value)
                self.assertEqual(set(found), set(declarations))
                for name, (expected, category, rationale) in declarations.items():
                    self.assertEqual(found[name], expected)
                    self.assertEqual(category, 'configured policy')
                    self.assertTrue(rationale.strip())

    def test_started_evidence_alias_is_not_a_second_declaration(self):
        evidence = ast.parse((ROOT / 'evidence_records.py').read_text(encoding='utf-8'))
        runtime = ast.parse((ROOT / 'runtime.py').read_text(encoding='utf-8'))
        self.assertEqual(inventory(evidence), {'MAX_STARTED_EVIDENCE_BYTES': '65536'})
        self.assertNotIn('MAX_STARTED_EVIDENCE_BYTES', inventory(runtime))

    def test_gate_declared_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for filename, declarations in GATE_DECLARATIONS.items():
            with self.subTest(file=filename):
                for expression, category, rationale in declarations.values():
                    self.assertIn(category, categories)
                    self.assertTrue(rationale.strip())
                tree = ast.parse((GATE_ROOT / filename).read_text(encoding='utf-8'))
                self.assertEqual(inventory(tree), {
                    name: entry[0] for name, entry in declarations.items()
                })

    def test_configured_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        for owner, declarations in CONFIGURED.items():
            node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
            self.assertEqual({field.target.id: ast.unparse(field.value) for field in node.body
                              if isinstance(field, ast.AnnAssign)},
                             {name: entry[0] for name, entry in declarations.items()})
            for _, category, rationale in declarations.values():
                self.assertEqual(category, 'configured policy')
                self.assertTrue(rationale.strip())

    def test_every_gate_module_with_bounds_is_inventoried(self):
        missing = {}
        for path in sorted(GATE_ROOT.glob('*.py')):
            if path.name in GATE_DECLARATIONS:
                continue
            found = inventory(ast.parse(path.read_text(encoding='utf-8')))
            if found:
                missing[path.name] = sorted(found)
        self.assertEqual(missing, {}, 'Undeclared gate bounds: %r' % (missing,))

    def test_config_module_bounds_are_fully_declared(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        found = inventory(tree)
        declared = set(CONFIG_MODULE_BOUNDS)
        for special in (CHECK_RETENTION, HISTORY_TEMPORARIES, DECISION_PROJECTION,
                        DIAGNOSTIC_SAMPLES, DECISION_RECONCILIATION, SMOKE_RETENTION_SCAN):
            declared.update(name for name in special if name in found)
        self.assertEqual(set(found), declared)
        categories = {'mechanism', 'format', 'safety'}
        for name, (expression, category, rationale) in CONFIG_MODULE_BOUNDS.items():
            with self.subTest(bound=name):
                self.assertEqual(found[name], expression)
                self.assertIn(category, categories)
                self.assertTrue(rationale.strip())

    def test_vcs_poll_and_ci_wake_fallbacks_match_configured_policy_defaults(self):
        policy = inventory(ast.parse((ROOT / 'vcs_poll_policy.py').read_text(encoding='utf-8')))
        wake = inventory(ast.parse((ROOT / 'ci_wake.py').read_text(encoding='utf-8')))
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        assignment = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'vcs'
                                  for target in node.targets)
                          and isinstance(node.value, ast.Dict))
        defaults = {ast.literal_eval(key): ast.unparse(value)
                    for key, value in zip(assignment.value.keys, assignment.value.values)
                    if isinstance(key, ast.Constant)}
        self.assertEqual(
            {name: policy[name] for name in ('MAX_CI_BRANCHES', 'POLL_CALL_TIMEOUT_CAP',
                                              'POLL_FETCH_TIMEOUT_CAP')},
            {'MAX_CI_BRANCHES': defaults['poll_max_branches'],
             'POLL_CALL_TIMEOUT_CAP': defaults['poll_status_timeout_seconds'],
             'POLL_FETCH_TIMEOUT_CAP': defaults['poll_fetch_timeout_seconds']})
        self.assertEqual(wake['DEFAULT_CI_REWAKE_SECONDS'], defaults['ci_rewake_seconds'])

    def test_check_retention_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'check_retained')
        self.assertEqual(ast.unparse(field.value), CHECK_RETENTION['check_retained'][0])
        self.assertEqual(inventory(tree)['MAX_CHECK_RETAINED'], CHECK_RETENTION['MAX_CHECK_RETAINED'][0])
        self.assertEqual([entry[1] for entry in CHECK_RETENTION.values()], ['configured policy', 'safety'])
        self.assertTrue(all(entry[2].strip() for entry in CHECK_RETENTION.values()))

    def test_history_temporary_policy_and_safety_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body
                     if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'history_temporaries')
        self.assertEqual(ast.unparse(field.value), HISTORY_TEMPORARIES['history_temporaries'][0])
        self.assertEqual(inventory(tree)['MAX_HISTORY_TEMPORARIES'],
                         HISTORY_TEMPORARIES['MAX_HISTORY_TEMPORARIES'][0])
        default = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name)
                               and target.id == 'DEFAULT_HISTORY_TEMPORARIES'
                               for target in node.targets))
        self.assertEqual(ast.unparse(default.value),
                         HISTORY_TEMPORARIES['DEFAULT_HISTORY_TEMPORARIES'][0])
        self.assertEqual([HISTORY_TEMPORARIES[key][1]
                          for key in ('history_temporaries', 'DEFAULT_HISTORY_TEMPORARIES',
                                      'MAX_HISTORY_TEMPORARIES')],
                         ['configured policy', 'configured policy', 'safety'])
        self.assertTrue(all(entry[2].strip() for entry in HISTORY_TEMPORARIES.values()))

    def test_smoke_retention_scan_budget_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        fields = {node.target.id: ast.unparse(node.value) for node in limits.body
                  if isinstance(node, ast.AnnAssign)}
        modules = {target.id: ast.unparse(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   for target in node.targets if isinstance(target, ast.Name)}
        for name, (expression, category, rationale) in SMOKE_RETENTION_SCAN.items():
            with self.subTest(bound=name):
                self.assertEqual(fields.get(name, modules.get(name)), expression)
                self.assertEqual(category, 'configured policy')
                self.assertTrue(rationale.strip())
        self.assertEqual(
            {name for name in SMOKE_RETENTION_SCAN if name.startswith('smoke_')},
            {'smoke_retention_projects', 'smoke_retention_pairs', 'smoke_retention_refs',
             'smoke_retention_entries', 'smoke_retention_metadata_bytes',
             'smoke_retention_report_bytes'} & set(fields))

    def test_decision_projection_policy_and_safety_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body
                     if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'decision_projection_records')
        self.assertEqual(ast.unparse(field.value), DECISION_PROJECTION['decision_projection_records'][0])
        self.assertEqual(inventory(tree)['MAX_DECISION_PROJECTION_RECORDS'],
                         DECISION_PROJECTION['MAX_DECISION_PROJECTION_RECORDS'][0])
        default = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name)
                               and target.id == 'DEFAULT_DECISION_PROJECTION_RECORDS'
                               for target in node.targets))
        self.assertEqual(ast.unparse(default.value),
                         DECISION_PROJECTION['DEFAULT_DECISION_PROJECTION_RECORDS'][0])
        insights = ast.parse((ROOT / 'insights.py').read_text(encoding='utf-8'))
        constructor = next(node for node in insights.body
                           if isinstance(node, ast.ClassDef) and node.name == 'Insights')
        initializer = next(node for node in constructor.body
                           if isinstance(node, ast.FunctionDef) and node.name == '__init__')
        index = [arg.arg for arg in initializer.args.kwonlyargs].index(
            'decision_projection_records')
        self.assertEqual(ast.unparse(initializer.args.kw_defaults[index]),
                         DECISION_PROJECTION['decision_projection_records'][0])
        self.assertEqual([DECISION_PROJECTION[key][1]
                          for key in ('decision_projection_records',
                                      'DEFAULT_DECISION_PROJECTION_RECORDS',
                                      'MAX_DECISION_PROJECTION_RECORDS')],
                         ['configured policy', 'configured policy', 'safety'])
        self.assertTrue(all(entry[2].strip() for entry in DECISION_PROJECTION.values()))

    def test_decision_reconciliation_policy_and_safety_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body
                     if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'decision_reconciliation_bytes')
        self.assertEqual(ast.unparse(field.value),
                         DECISION_RECONCILIATION['decision_reconciliation_bytes'][0])
        self.assertEqual(inventory(tree)['MAX_DECISION_RECONCILIATION_BYTES'],
                         DECISION_RECONCILIATION['MAX_DECISION_RECONCILIATION_BYTES'][0])
        default = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name)
                               and target.id == 'DEFAULT_DECISION_RECONCILIATION_BYTES'
                               for target in node.targets))
        self.assertEqual(ast.unparse(default.value),
                         DECISION_RECONCILIATION['DEFAULT_DECISION_RECONCILIATION_BYTES'][0])
        insights = ast.parse((ROOT / 'insights.py').read_text(encoding='utf-8'))
        constructor = next(node for node in insights.body
                           if isinstance(node, ast.ClassDef) and node.name == 'Insights')
        initializer = next(node for node in constructor.body
                           if isinstance(node, ast.FunctionDef) and node.name == '__init__')
        index = [arg.arg for arg in initializer.args.kwonlyargs].index(
            'decision_reconciliation_bytes')
        self.assertEqual(ast.unparse(initializer.args.kw_defaults[index]),
                         DECISION_RECONCILIATION['decision_reconciliation_bytes'][0])
        self.assertEqual([DECISION_RECONCILIATION[key][1]
                          for key in ('decision_reconciliation_bytes',
                                      'DEFAULT_DECISION_RECONCILIATION_BYTES',
                                      'MAX_DECISION_RECONCILIATION_BYTES')],
                         ['configured policy', 'configured policy', 'safety'])
        self.assertTrue(all(entry[2].strip() for entry in DECISION_RECONCILIATION.values()))

    def test_diagnostic_sample_policy_and_safety_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        fields = {field.target.id: ast.unparse(field.value) for field in limits.body
                  if isinstance(field, ast.AnnAssign)}
        self.assertEqual(fields['decision_receipt_diagnostics'],
                         DIAGNOSTIC_SAMPLES['decision_receipt_diagnostics'][0])
        self.assertEqual(fields['queue_observations'],
                         DIAGNOSTIC_SAMPLES['queue_observations'][0])
        for key in ('DEFAULT_DECISION_RECEIPT_DIAGNOSTICS',
                    'MAX_DECISION_RECEIPT_DIAGNOSTICS',
                    'DEFAULT_QUEUE_OBSERVATIONS', 'MAX_QUEUE_OBSERVATIONS'):
            assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == key
                                      for target in node.targets))
            self.assertEqual(ast.unparse(assignment.value), DIAGNOSTIC_SAMPLES[key][0])
        self.assertEqual([DIAGNOSTIC_SAMPLES[key][1] for key in (
            'decision_receipt_diagnostics', 'DEFAULT_DECISION_RECEIPT_DIAGNOSTICS',
            'MAX_DECISION_RECEIPT_DIAGNOSTICS', 'queue_observations',
            'DEFAULT_QUEUE_OBSERVATIONS', 'MAX_QUEUE_OBSERVATIONS')],
                         ['configured policy', 'configured policy', 'safety',
                          'configured policy', 'configured policy', 'safety'])
        self.assertTrue(all(entry[2].strip() for entry in DIAGNOSTIC_SAMPLES.values()))

    def test_configured_raw_settings_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        for table, declarations in CONFIGURED_SETTINGS.items():
            node = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == table
                                for target in node.targets)
                        and isinstance(node.value, ast.Dict))
            defaults = {ast.literal_eval(key): ast.unparse(value)
                        for key, value in zip(node.value.keys, node.value.values)
                        if isinstance(key, ast.Constant)}
            self.assertEqual({name: defaults[name] for name in declarations},
                             {name: entry[0] for name, entry in declarations.items()})
            for _, category, rationale in declarations.values():
                self.assertEqual(category, 'configured policy')
                self.assertTrue(rationale.strip())

    def test_detector_scope_and_vocabulary(self):
        names = ['MAX_ITEMS', 'READ_LIMIT', 'READ_CAP', 'READ_TIMEOUT',
                 'READ_TIMEOUT_MS', 'READ_SECONDS', 'READ_DAYS', 'READ_DEPTH',
                 'READ_ENTRIES', 'READ_BYTES', 'READ_ROWS', 'READ_COUNT']
        source = '\n'.join(f'{name} = 2 * 1024' for name in names) + '''
_PRIVATE_BYTES: int = -8
FIRST_CAP = SECOND_CAP = 3
MAX_LABEL = "not numeric"
MAX_FLAG = True
UNRELATED = 42
INTERVAL_MAX = 10
TIMEOUT = 10
ONLY_TYPE_BYTES: int

def nested():
    MAX_NESTED = 1
class Container:
    MAX_CLASS = 2
if True:
    MAX_CONDITIONAL = 3
'''
        expected = dict.fromkeys(names, '2 * 1024')
        expected.update(_PRIVATE_BYTES='-8', FIRST_CAP='3', SECOND_CAP='3', INTERVAL_MAX='10')
        self.assertEqual(inventory(ast.parse(source)), expected)

    def test_detector_exposes_additions_removals_and_expression_changes(self):
        original = inventory(ast.parse('MAX_ITEMS = 2 * 1024'))
        for source in ('MAX_ITEMS = 2 * 1024\nNEW_CAP = 5', '',
                       'MAX_ITEMS = 2048'):
            with self.subTest(source=source):
                self.assertNotEqual(inventory(ast.parse(source)), original)
        with self.assertRaisesRegex(AssertionError, 'Duplicate bound declaration'):
            inventory(ast.parse('MAX_ITEMS = 1\nMAX_ITEMS = 2'))


    def test_resource_call_literal_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for filename, declarations in CALLS.items():
            with self.subTest(file=filename):
                for expression, category, rationale in declarations.values():
                    self.assertIn(category, categories)
                    self.assertTrue(rationale.strip())
                tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
                self.assertEqual(call_inventory(tree), {
                    site: entry[0] for site, entry in declarations.items()
                })

    def test_every_module_with_call_literals_is_inventoried(self):
        for path in sorted(ROOT.glob('*.py')):
            with self.subTest(file=path.name):
                found = call_inventory(ast.parse(path.read_text(encoding='utf-8')))
                if found:
                    self.assertIn(path.name, CALLS)

    def test_call_detector_scope_and_mismatch_detection(self):
        source = '''
def outer():
    run(timeout=5, maximum=2)
    run(timeout=6)
    read_json(max_bytes=2 ** 10)
    run(timeout_seconds=20)
    other(max_files=3, max_entries=4, max_depth=2)
    page(limit=1)
    run(timeout=LIMIT)

def sibling():
    run(timeout=7)
'''
        expected = {
            ('outer', 'run', 'timeout', 0): '5',
            ('outer', 'run', 'maximum', 0): '2',
            ('outer', 'run', 'timeout', 1): '6',
            ('outer', 'read_json', 'max_bytes', 0): '2 ** 10',
            ('outer', 'run', 'timeout_seconds', 0): '20',
            ('outer', 'other', 'max_files', 0): '3',
            ('outer', 'other', 'max_entries', 0): '4',
            ('outer', 'other', 'max_depth', 0): '2',
            ('outer', 'page', 'limit', 0): '1',
            ('sibling', 'run', 'timeout', 0): '7',
        }
        self.assertEqual(call_inventory(ast.parse(source)), expected)
        for changed in (
                source + '\nnew_call(timeout=1)\n',
                source.replace('run(timeout=6)', 'run(timeout=8)'),
                source.replace('run(timeout=5, maximum=2)', 'run(timeout=5)'),
                source.replace('def sibling():', 'def renamed():')):
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                self.assertEqual(call_inventory(ast.parse(changed)), expected)


if __name__ == '__main__':
    unittest.main()
