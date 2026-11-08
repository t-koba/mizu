"""Partial runtime-bound inventory, not a general numeric-literal linter.

The `src/mizu` files below cover named, direct module-level Assign/AnnAssign
bounds and numeric literals nested anywhere in selected resource-call keyword
expressions, including local deadline arithmetic and caller-default forwarding.
The gate inventory covers `scripts/check.py`'s host-cost, output
and artifact bounds; its enforced host-cost choices are configured policy
defaults, while its scope reserves and output/artifact bounds are fixed safety
ceilings.
The editor search byte, match and excerpt defaults are configured policy in
`[editor]`, captured per export. The per-file `PREVIEW_BYTES` read bound, shared
maximum excerpt, strict settings-file read cap and MAX_FRAME-derived canonical
response cap remain fixed safety/framing bounds, checked against their uses.
The `vcs.py` provider response-shape bounds are fixed mechanism and safety
ceilings for untrusted content; it owns the shared proposal cursor, generated
proposal-ID suffix, and Insight-title format bounds. Upstream-ref path slicing
derives its prefix length from `REF_PREFIX`.
The proposal poll's surfaced diagnostic preview is a fixed safety cap; its
pagination behavior otherwise follows the VCS schema without a provider guess.
The `project.py` git-tree import time bounds are configured policy; its
remaining output caps are fixed safety ceilings. The `insights.py` module
constants are fixed record and evidence safety bounds; its explicit-history
result count is configured policy with a fixed ceiling, and discovery remains
unbounded by that count. Constructor policy defaults include policy supplied by
`Project`, including history output and the malformed-receipt diagnostic sample;
literal bound-parameter defaults are
classified separately below. The `runtime.py` literals are fixed run-local memory, one-row probe and public page
bounds; its configurable admission and dispatch choices are `Limits` and
`[dispatch_evidence]` fields, including the run-local queue-observation sample.
`evidence_records.py` declares the started.json authorization-read ceiling that
also trims that sample; runtime re-exports it only as an alias.
The `acceptance.py` retained-evidence ceiling, half-envelope scope cap,
cache-diagnostic entry cap and public invalid-producer diagnostic sample cap,
CI observation total-row/prompt caps and wake-branch ceiling, and `ci_waits.py`
diagnostic page caps are fixed safety bounds. The scope cap reserves half the receipt
envelope for proof metadata; the complete receipt still uses the full ceiling. The acceptance
sample is capped at 32 rows while total/omitted report coverage and the complete
internal exclusion set remain intact. `check_jobs.py` names its
check-summary/evidence row caps,
identity suffix, text-preview widths and shared capture-error excerpt; its
caller-visible page and command-array limits remain checked against their
existing schema declarations. CI branch/check retention defaults are configured policy owned
by `[vcs] ci_cache_max_branches` and `ci_cache_max_checks_per_branch`, within
the unchanged total-row envelope; `ci_waits.py`'s serialized check-set size is
a mechanism contract;
`ci_wake.py`'s re-wake default is configured policy owned by
`[vcs] ci_rewake_seconds`, and its record version is format, outside the
name-scoped detector.
The `lock_diagnostics.py`
constants are fixed journal-line row and numeric-field format bounds; the
opt-in collection is policy, but it introduces no host-cost numeric bound.
The `cli.py` module-level inventory is empty; its numeric page defaults are
classified in `CALLS` as command-line presentation policy, not module constants.
The `config.py` module-level candidates are closed over the existing
policy/default dictionaries and `CONFIG_MODULE_BOUNDS`; symbolic `min` and
canonical-length
derivations use only their explicitly allowlisted operands in `SYMBOLIC_BOUNDS`.
Event polling and whole-file artifact reads retain configured defaults;
`[services]` owns the systemd restart and stop deadline defaults (15 and 45
seconds); include, role-policy, resource, CI-wake-branch and workspace-depth
ceilings are mechanism or safety bounds; protocol integers and wait-seconds
retain their format ranges.
The `web_worker.py` include-envelope bound derives from the fixed include
byte ceiling. `lineage.py`'s full-ref length derives from its fixed prefix and
branch-name format bound. `work_items.py` page, cursor, row, projection,
revision, delivered-token and sidecar bounds are fixed mechanism or safety
contracts; its delivered-token set covers prior progress plus one opening page,
and its page-byte aliases are inventoried against shared declared bases rather
than treated as new independent numeric policies.
The `work_item_read_view.py` snapshot, file and parsed-item retention budgets
are configured policy under `[limits]`; misses repeat validated reads. Their
exact defaults and uses are checked separately; the path-key cache stays fixed.
The configured `[limits].wait_events_limit` applies independently to each
structured wait/obligation delivery page; readiness existence probes still need
only one candidate and the fixed 100-row route ceilings remain unchanged.
The `fs.py` and `platform.py` framing, read-page, script-input, JSON-depth,
spawn-reap bounds are fixed mechanism or safety. POSIX pipe idle polling is
configured policy owned by `[limits].pipe_poll_interval_ms` (default 100 ms,
range 1–500 ms), captured explicitly by reader threads. Data and EOF wake
immediately; the ceiling preserves subsecond idle cancellation. The bridge timeout fallback
is shared between `bridge.py` and `mcp_proxy.py` for incomplete configs; normal
bridge configs carry a deadline derived from model-request policy. The MCP
proxy receive chunk bounds transient allocation while `MAX_FRAME` caps the full
response. Lock waits are configured
host/load policy owned by `[limits].lock_timeout_seconds` (default 30 s) and
`[limits].lock_retry_interval_ms` (default 50 ms, clipped to the deadline). The widened
character-count and maximum suffixes match the read-page and script-input names,
so they are ordinary declarations; `EXTRA_CANDIDATES` holds the probe
header-pair count, model-catalog provider-command output ceiling, and runtime
wrap-up allowance defaults, whose names fall outside the pattern.
MCP and persistent-exec cleanup walls use `[sandbox].cleanup_timeout_seconds`;
project-wide cleanup uses `[sandbox].bulk_cleanup_timeout_seconds`.
Checkpoint-image inspection uses `[sandbox].checkpoint_inspect_timeout_seconds`;
Systemd-analyze verification uses `[services].systemd_verify_timeout_seconds`.
Their output reads are safety.
The `delegation_jobs.py` byte ceilings are private format/safety limits for the
not-yet-wired single-child delegation pilot. Its 1024-job history cap is shared
project-state capacity policy: exhaustion refuses new starts rather than
forgetting keys. The module has no `src/mizu` consumer or configuration route;
defer an operator setting until the pilot integration can actually consume it,
rather than expose an inert option.
The `storage.py` artifact-retention default is CLI-controlled policy, while the
day conversion and automatic-prune/audit read limits are fixed mechanism or
safety bounds. Restore's fixed member-read chunk and archive-member count bound
transient resources while its separate `max_bytes` setting limits total restored
bytes. The `engine_config.py`
reviewed-resource byte and tree-entry ceilings are
fixed safety bounds; the 4096-entry traversal budget also bounds file count.
The `drivers.py` event-stream default is the configured
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
paid smoke wall cap, and the recovery-probe execution ceilings are fixed
mechanism or safety bounds. Recovery-probe executable hashing has a separate
host I/O policy under `[limits].probe_executable_bytes` (default 256 MiB), with
a fixed 64 KiB streaming buffer. Selection timer ceilings are configured policy owned by
`[limits].selection_max_interval_seconds` (default 31,536,000); saved intervals
use the TOML integer representation and timestamps retain the year-9999 format
bound independently of the current policy cap. Selection retention is also policy:
`MAX_TERMINAL_CONTRIBUTIONS` and
`MAX_PROBE_EVENTS` are `configured policy` defaults for the optional
data-dir `selection/policy.json`; absence keeps 256/8 and malformed values
refuse selection. The
selection module's `DEFAULT_ON_ERROR_RETRIES` and `DEFAULT_CLASSIFIER_INPUTS`
are configured schema defaults declared separately; the per-rule and
per-classifier tables are their owning configuration. Every current
`src/mizu` module with a detected candidate is covered here (`config.py` has
its own field-scoped table), so a new module with such a bound fails until it
is declared. The candidate vocabulary also includes nonstandard bound-bearing
suffixes (`_DEFAULT`, `_MIN`, `_MAXIMUM`, `_SEC`, `_SAMPLE`, `_BELOW`, and
`_OVERHEAD`) so operational defaults and diagnostic caps are not missed merely
because their names do not begin with `MAX_` or `DEFAULT_`. The selected
work-item scan also records cursor arity, sidecar delivery-revision floor and
cache-witness tuple shape as format/mechanism invariants, not resource ceilings.
Other schema/version markers without resource-bound behavior remain outside this
detector. Every module with a numeric literal in the selected resource-call keyword
expressions is covered in `CALLS`, so newly added local arithmetic, cache/queue
capacities and caller-default forwarding at those sites fail until declared.
The module and call inventories remain name- and scope-restricted and therefore
partial; absence from the tables is not evidence that a value is not a decision.
Expressions are inventoried without evaluating source. The call detector covers
local arithmetic only when it appears in a selected bound-bearing keyword; the
separate timed-wait table covers numeric positional arguments to selected
`wait`/`sleep` calls. Explicit maximum arguments to the shared `bounded()` JSON
helper are also inventoried across source modules. These tests still do not lint
arbitrary numeric literals, alias-only values outside declared names and these
selected calls, nested assignments, symbolic non-`range` loop bounds, positional
arguments outside those selected sets, or call keywords outside the listed set.
The exact range tables do not infer other loop forms. Expand coverage as the
behavior-based inventory reaches other owners.
The configured slice checks Usage, SchedulerEvidence, CiRequestEvidence,
PublicationPolicy, PublicationEvidence and WorkspaceCache field defaults, VCS
polling fallback defaults, the retained-check Limits field and ceiling, and
explicitly listed raw web and VCS settings; the decision-history recovery ceiling
is checked alone, not as a full work_items.py inventory. Validation is covered
by focused config tests. Classification records current ownership, not approval
to leave policy in code. `format` is reserved for intrinsic wire or schema
ranges; `safety` is a defensive ceiling on untrusted or oversized input,
including a product-chosen validation maximum; `mechanism` is a fixed internal
or public contract, including a retry-loop bound; `policy` is an operator
decision not yet configurable; `configured policy` is its migrated form: an
operator-configurable setting or config-owned default with a documented default
and, where a field exists, a validated range. The declaration tables assert the
field or default expression, and the focused config tests exercise the range.
Safety classifications here are defensive read or validation bounds, not
intrinsic format limits.

The parameter-default slice recursively scans every Python file under `src/mizu`
for named sync/async functions and methods, including nested scopes. It matches
whole underscore-delimited bound words (see PARAMETER_BOUND), not arbitrary
substrings. It records exact file, class/function scope, parameter and unparsed
numeric expression in PARAMETER_DEFAULTS. Positional-only and positional defaults
are right-aligned together; keyword-only defaults are paired individually.
Numeric means int/float literals (not bool), unary expressions and arithmetic
composed solely of them, as in the other detectors; aliases, calls, None,
lambdas, dataclass fields and names outside the vocabulary are excluded. Exact
inventory equality rejects additions, removals and expression changes, and
repeated detected scope/parameter keys are refused rather than overwritten.
Zero offsets and depth counters are mechanism state, not capacity decisions.
Caller-selected page sizes and host timing remain policy even when a separate
validation ceiling exists. `configured policy` names an existing config/CLI
owner; `policy` marks a caller-controlled or still hard-coded choice, not a
claim that it is configured. The inventories protect current declarations and
defaults; where source evidence establishes an operator-owned choice, this audit
may add an explicit setting while preserving the previous behavior by default.
It does not infer host-adapter contracts.

The direct-range slice inventories every numeric positional argument to
built-in `range(...)` calls by file, scope, argument and occurrence. A separate
exact table classifies all currently symbolic `range` arguments across
`src/mizu`, including their configuration owners and data-derived bounds.
Arbitrary loop conditions, slices, aliases and numeric literals in other
contexts remain outside these inventories. A direct-comparison slice records
operands of magnitude at least 3 in `check_jobs.py`, `codex.py`, `acceptance.py`,
`ci_acceptance.py`, `ci_requests.py`, `ci_waits.py`, `ci_wake.py`,
`insights.py`, `proposal_waits.py`, `selection.py`, `usage.py`, `config.py`,
`runtime.py`, `project.py`, `services.py`, `persistent_exec.py`, `cli.py` and
`vcs.py`; numeric-only arithmetic operands, including bounded integer powers,
are evaluated without source execution. The `services.py` entries pin
Windows Task Scheduler's one-minute minimum and 31-day maximum repetition
interval. The `admission.py` slice uses
magnitude 2 to include its exact two-lock resource ceiling. This is not a global
numeric comparison linter.
"""
import ast
import math
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'src' / 'mizu'
GATE_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
CANDIDATE = re.compile(
    r'(?:MAX_\w+|DEFAULT_\w+|\w+_(?:LIMIT|CAP|TIMEOUT\w*|SECONDS|DAYS|DEPTH|'
    r'ENTRIES|BYTES|ROWS|COUNT|MULTIPLIER|PER_RUN|CHARS|MAX|DEFAULT|MIN|MAXIMUM|'
    r'SEC|SAMPLE|BELOW|OVERHEAD))\Z'
)
# Module-level names outside CANDIDATE that still bound public resource behavior.
EXTRA_CANDIDATES = {
    'platform.py': frozenset({'LOCK_RETRY_INTERVAL_MS', 'PIPE_POLL_INTERVAL_MS'}),
    'snapshot_manifest.py': frozenset({'_WINDOWS_EXTENDED_PATH_UNITS',
                                       '_MAX_FILES_WHITESPACE_RUN'}),
    'model_catalog.py': frozenset({'MAXIMUM'}),
    'web.py': frozenset({'PROBE_MAX_HEADERS'}),
    'work_items.py': frozenset({'_SIDECAR_MAX_BYTES'}),
    'runtime.py': frozenset({'WRAP_UP_REQUESTS', 'WRAP_UP_TOOLS'}),
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
        'MAX_ACCEPTANCE_SCOPE_BYTES': (
            'MAX_BYTES // 2', 'safety',
            'acceptance / reserve half the receipt envelope for scope metadata; '
            'the complete receipt remains bounded by MAX_BYTES'),
        'MAX_INVALID_PRODUCER_DIAGNOSTIC_ENTRIES': (
            '32', 'safety',
            'acceptance / fixed public diagnostic sample envelope for untrusted entries; '
            'full internal exclusions and total/omitted coverage are preserved'),
        'MAX_CACHE_DIAGNOSTIC_ENTRIES': (
            '2', 'safety',
            'acceptance / cap retained cache diagnostics to the input/output phase pair'),
    },
    'classification.py': {
        'MAX_CLASSIFICATION_MODEL_LABEL_CHARS': (
            '256', 'safety',
            'classification / bound untrusted driver identity labels in cached model summaries'),
        'MAX_CLASSIFICATION_MODEL_INTEGER': (
            '2 ** 53', 'format',
            'classification / inclusive binary64 integer-representation edge for model-count JSON values'),
        'MAX_CLASSIFICATION_MODEL_SUMMARY_BYTES': (
            '4096', 'safety',
            'classification / bound the copied model-usage summary before retaining it'),
        'MAX_CLASSIFICATION_CACHE_BYTES': (
            '65536', 'safety',
            'classification / per-role cache read and canonical-value ceiling'),
    },
    'bridge.py': {
        'BRIDGE_BACKLOG_LIMIT': (
            '16', 'safety',
            'bridge / bounded pending accept backlog for each per-run local listener'),
        'DEFAULT_BRIDGE_TIMEOUT_MS': (
            '60000', 'mechanism',
            'bridge / fallback for incomplete local configs; runtime configs carry the configured model-request deadline'),
    },
    'mcp_proxy.py': {
        'MCP_PROXY_RECV_CHUNK_BYTES': (
            '65536', 'safety',
            'MCP proxy / transient socket-read allocation bound; MAX_FRAME independently caps the full reply'),
    },
    'evidence_records.py': {
        'MAX_STARTED_EVIDENCE_BYTES': (
            '65536', 'safety',
            'evidence records / fixed started.json authorization-read ceiling; file_bytes may lower it'),
    },
    'ci_observation.py': {
        'MAX_CI_FAILURES': (
            '32', 'safety',
            'CI observation / bounded flattened current-failure rows for the prompt'),
        'MAX_CI_CHECKS_PER_BRANCH': (
            'DEFAULT_CI_CACHE_MAX_CHECKS_PER_BRANCH', 'configured policy',
            'CI observation / fallback for vcs.ci_cache_max_checks_per_branch'),
        'MAX_CI_FAILURE_BRANCHES': (
            'DEFAULT_CI_CACHE_MAX_BRANCHES', 'configured policy',
            'CI observation / fallback for vcs.ci_cache_max_branches'),
        'MAX_CI_WAKE_BRANCHES': (
            'MAX_CI_WAKE_EXTRA_BRANCHES + 1', 'safety',
            'CI observation / one required ci_branch plus the fixed extra-branch ceiling'),
        'MAX_CI_WAKE_ROWS': (
            'MAX_CI_CACHE_ROWS', 'safety',
            'CI observation / wake projection covers the entire fixed per-map safety envelope'),
    },
    'web_worker.py': {
        'MAX_WEB_WORKER_INPUT_BYTES': (
            '3 * MAX_INCLUDE_BYTES', 'safety',
            'web worker / bounded canonical request frame derived from the fixed include-byte ceiling'),
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
        '_INTENT_NULL_OVERHEAD': (
            '14', 'format',
            'async delegation / canonical JSON byte overhead for the explicit intent:null member'),
        'MAX_JOBS': (
            '1024', 'policy',
            'async delegation / shared project-state tombstone capacity; the private store has no '
            'src/mizu consumer yet, so defer a configuration setting until pilot integration'),
        'MAX_BINDING_BYTES': (
            '8192', 'safety',
            'async delegation / bounded active binding and provisioning-intent inventory'),
        'MAX_STATE_BYTES': (
            '1024 * 1024 + MAX_BINDING_BYTES + _INTENT_NULL_OVERHEAD * MAX_JOBS', 'safety',
            'async delegation / bounded ledger read including one active inventory and the '
            'per-job serialized intent slot; oversized state is refused, never reset'),
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
            '_platform.LOCK_TIMEOUT', 'configured policy',
            'fs / standalone default for [limits].lock_timeout_seconds; Config execution scope owns overrides'),
    },
    'snapshot_manifest.py': {
        '_WINDOWS_EXTENDED_PATH_UNITS': (
            '32767', 'format',
            'snapshot manifest / Windows extended-length filesystem path limit in UTF-16 code units'),
        '_MAX_ENTRY_JSON_BYTES': (
            '4096', 'safety',
            'snapshot manifest / bounded one-member JSON entry before decoding untrusted fields'),
        '_MAX_FILES_WHITESPACE_RUN': (
            '1024', 'safety',
            'snapshot manifest / bound formatting-only whitespace scan between file-map tokens'),
    },
    'platform.py': {
        'PIPE_POLL_INTERVAL_MS': (
            '100', 'configured policy',
            'platform / shared default for [limits].pipe_poll_interval_ms; reader threads capture the execution policy'),
        'LOCK_RETRY_INTERVAL_MS': (
            '50', 'configured policy',
            'platform / shared default for [limits].lock_retry_interval_ms; fs forwards the execution policy'),
        'LOCK_TIMEOUT': (
            '30', 'configured policy',
            'platform / shared default for [limits].lock_timeout_seconds; fs forwards the execution policy'),
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
            'storage audit / generic reference-JSON cap; snapshot manifests have a separate bound'),
        'RESTORE_CHUNK_BYTES': (
            '1048576', 'safety',
            'storage restore / fixed streamed-member read chunk bounds transient memory; '
            'max_bytes separately limits total restored bytes'),
        'RESTORE_MEMBER_COUNT_MAX': (
            '100000', 'safety',
            'storage restore / fixed ceiling bounds filesystem work for untrusted archives; '
            'max_bytes separately limits total member bytes'),
        'MAX_SNAPSHOT_GOAL_JSON_EXPANSION': (
            '6', 'format',
            'storage audit / canonical JSON can escape one UTF-8 control byte as six ASCII bytes'),
        '_SNAPSHOT_AUDIT_BYTES': (
            '_AUDIT_JSON_BYTES + MAX_PROJECT_GOAL_BYTES * MAX_SNAPSHOT_GOAL_JSON_EXPANSION',
            'safety',
            'storage audit / fixed cap accepts historical goals through worst-case JSON expansion; '
            'unrelated references keep the generic cap'),
        'AUTOMATIC_PRUNE_INTERVAL_DEFAULT': (
            '86400', 'configured policy',
            'automatic prune / unchanged cadence default when the validated [limits] override is omitted'),
        '_AUDIT_SAMPLE': (
            '100', 'safety',
            'storage audit / bounded diagnostic samples retain full counts and truncation markers'),
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
    'insight_archive.py': {
        'MAX_SQLITE_INTEGER': (
            '9223372036854775807', 'format',
            'insight archive / signed 64-bit SQLite INTEGER ceiling for revisions '
            'and cursor high-water'),
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
        'WRAP_UP_REQUESTS': (
            '3', 'configured policy',
            'runtime / fallback for [limits] wrap_up_requests; project default with per-role override'),
        'WRAP_UP_TOOLS': (
            '5', 'configured policy',
            'runtime / fallback for [limits] wrap_up_tools; project default with per-role override'),
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
    'work_item_resolution.py': {
        'MAX_RESOLUTION_BYTES': (
            '521', 'format', 'work items / exact maximum canonical wait-resolution member growth'),
    },
    'work_items.py': {
        'MAX_EVENT_BYTES': (
            'PREVIEW_BYTES + MAX_RESOLUTION_BYTES', 'mechanism',
            'work items / existing event ceiling plus the exact maximum wait-resolution member'),
        'MAX_REVISIONS': (
            '256', 'mechanism',
            'work items / revision-chain format bound including the immutable seed'),
        'MAX_OPEN_PAGE_ROWS': (
            '100', 'mechanism', 'work items / public open-page row contract'),
        'MAX_DELIVERED_WAIT_RESOLUTION_TOKENS': (
            '3 * MAX_OPEN_PAGE_ROWS', 'mechanism',
            'work items / prior 200-token progress bound plus one 100-row opening page'),
        'MAX_OPEN_CURSOR_CHARS': (
            '128', 'mechanism', 'work items / opaque cursor format bound'),
        'MAX_OPEN_ROW_BYTES': (
            '8192', 'safety', 'work items / bounded one-row response projection'),
        'MAX_OPEN_PAGE_BYTES': (
            '1024 * 1024', 'safety', 'work items / bounded page response'),
        'MAX_CLOSED_PAGE_ROWS': (
            '100', 'mechanism', 'work items / public closed-page row contract'),
        'MAX_PROJECTION_BYTES': (
            'MAX_EVENT_BYTES + 256', 'safety',
            'work items / bounded canonical projection envelope around the shared text-preview ceiling'),
        'MAX_HISTORY_PAGE_BYTES': (
            'MAX_OPEN_PAGE_BYTES', 'safety',
            'work items / history response shares the public open-page byte ceiling'),
        'MAX_CLOSED_ROW_BYTES': (
            'MAX_OPEN_ROW_BYTES', 'safety',
            'work items / closed row shares the public open-row byte ceiling'),
        'MAX_CLOSED_PAGE_BYTES': (
            'MAX_OPEN_PAGE_BYTES', 'safety',
            'work items / closed response shares the public open-page byte ceiling'),
        '_SIDECAR_MAX_BYTES': (
            'PREVIEW_BYTES * 2 + 4096', 'safety',
            'work items / bounded structural sidecar read and write for two previews plus fixed metadata'),
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
        'PROBE_EXECUTABLE_HASH_CHUNK_BYTES': (
            '65536', 'mechanism',
            'recovery probes / fixed streaming hash buffer; bounds transient memory independently of executable size'),
        'MAX_SECONDS': (
            '90', 'safety',
            'recovery probes / fixed execution ceiling; configured budget and smaller limits still win'),
        'MAX_REQUESTS': (
            '2', 'safety',
            'recovery probes / fixed execution ceiling; real sponsor budget still admits'),
        'MAX_TOOLS': (
            '1', 'safety',
            'recovery probes / fixed execution ceiling; configured tool limit still wins'),
        'MAX_PROBE_EVENT_STREAM_BYTES': (
            '65536', 'safety',
            'recovery probes / fixed 64-KiB event-stream cap for one terminal availability challenge'),
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
        'MAX_ARTIFACT_CURSOR': ('256', 'format', 'VCS artifacts / tool-schema opaque cursor character bound'),
        'MAX_PROPOSAL_CURSOR': (
            '256', 'format',
            'VCS proposals / shared tool-schema opaque pagination cursor character bound'),
        'MAX_ARTIFACTS': ('256', 'mechanism', 'VCS artifacts / bounded true artifact entries'),
        'MAX_ARTIFACT_NAME': (
            '512', 'mechanism', 'VCS artifacts / bounded relative file-name chars'),
        'MAX_ARTIFACT_BYTES': (
            '(1 << 63) - 1', 'format', 'VCS artifacts / signed-64-bit adapter byte arithmetic'),
        'MAX_CHECKS': ('1024', 'mechanism', 'VCS status / adapter payload already capped'),
        'MAX_PROPOSALS': ('256', 'mechanism', 'VCS proposals / bounded external proposal rows'),
        'MAX_PROPOSAL_INSIGHT_ID_SUFFIX_CHARS': (
            '32', 'format',
            'VCS proposals / fixed lower-case SHA-256 hex suffix in generated Insight identity'),
        'MAX_PROPOSAL_INSIGHT_TITLE_CHARS': (
            '200', 'format',
            'VCS proposals / generated Insight title must fit its public title format'),
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
        'DEFAULT_VCS_POLL_INTERVAL_SECONDS': (
            '300', 'configured policy',
            'VCS polling / shared config default and defensive fallback interval'),
    },
    'smoke_retention.py': {
        'MAX_RECORD': ('128 * 1024', 'safety', 'smoke retention / bounded metadata-record read'),
        'MAX_DAYS': ('36500', 'safety', 'smoke retention / stored expiry-age validation maximum (100 years)'),
        'AUDIT_LIMIT': (
            '128', 'safety',
            'smoke retention / bounded maintenance-audit record read and retained event count'),
        '_AUTOMATIC_SMOKE_RETENTION_STATE_BYTES': (
            '4096', 'safety', 'smoke retention / bounded automatic due-state read'),
        'AUTOMATIC_SMOKE_RETENTION_INTERVAL_DEFAULT': (
            '86400', 'configured policy',
            'automatic smoke retention / unchanged cadence default when the validated [limits] override is omitted'),
        'AUTOMATIC_SMOKE_RETENTION_INTERVAL_MAX': (
            '2678400', 'safety',
            'configuration validation / maximum accepted automatic smoke-retention interval (31 days)'),
    },
    'check_jobs.py': {
        'PURPOSE_MAX': (
            '64', 'format',
            'check jobs / stored purpose label character bound'),
        'CHECK_INPUT_HASH_CHUNK_BYTES': (
            'MEBIBYTE_BYTES', 'safety',
            'check jobs / transient buffer bound while hashing one frozen input'),
        'COMMAND_EVIDENCE_OUTPUT_MULTIPLIER': (
            '8', 'safety',
            'check jobs / canonical JSON output-expansion allowance for bounded command evidence'),
        'COMMAND_EVIDENCE_ENVELOPE_BYTES': (
            'MEBIBYTE_BYTES', 'safety',
            'check jobs / bounded script and record-envelope allowance for command evidence'),
        'MAX_CHECK_JOB_ROLE_ID_SUFFIX_CHARS': (
            '24', 'format', 'check jobs / stable container-role identity suffix format'),
        'MAX_CHECK_JOB_PROJECTION_ROWS': (
            '32', 'safety', 'check jobs / bounded role-owned summary rows per projection'),
        'MAX_CHECK_JOB_COMMAND_PREVIEW_ITEMS': (
            '4', 'safety', 'check jobs / bounded commands shown per job summary'),
        'MAX_CHECK_JOB_SHORT_PREVIEW_CHARS': (
            '128', 'safety', 'check jobs / bounded short command and prior-run identity excerpts'),
        'MAX_CHECK_JOB_TEXT_PREVIEW_CHARS': (
            '512', 'safety', 'check jobs / bounded diagnostic and command-result text excerpts'),
        'MAX_CHECK_CAPTURE_ERROR_BYTES': (
            '256', 'safety', 'check jobs / bounded encoded shared-capture error excerpt'),
        'MAX_COMMAND_EVIDENCE_ID_HEX_CHARS': (
            '32', 'format', 'check jobs / public command-evidence identifier hex width'),
        'MAX_COMMAND_EVIDENCE_LIST_ROWS': (
            '32', 'safety', 'check jobs / bounded command-evidence listing rows'),
        'MAX_PRIOR_ROLE_REPORT_PREVIEW_CHARS': (
            '4000', 'safety', 'check jobs / bounded prior-role report summary and state excerpts'),
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
        'EDITOR_SEARCH_SETTINGS_BYTES': (
            '4096', 'safety', 'editor bundle / fixed three-integer search-settings read envelope'),
        'EDITOR_SEARCH_RESPONSE_BYTES': (
            'MAX_FRAME // 4', 'safety',
            'editor search / canonical response reserve for structured and escaped text copies plus MCP framing'),
        'EDITOR_SEARCH_SKIPPED_BYTES': (
            'EDITOR_SEARCH_RESPONSE_BYTES // 4', 'safety',
            'editor search / skipped-path diagnostics reserve so they cannot crowd out matches'),
        'BUNDLE_MANIFEST_BYTES': (
            '16 * 1024 * 1024', 'safety',
            'editor bundle / bounded manifest read'),
        'BUNDLE_CHANGES_BYTES': (
            '262144', 'safety',
            'editor bundle / bounded changed-text read'),
    },
    'engine_config.py': {
        'PROMPT_STATE_BYTES': (
            '65536', 'safety',
            'engine config / bounded session cursor envelope for row hashes and broker grant'),
        'MAX_SESSION_TOKENS': (
            '2 ** 63 - 1', 'format',
            'engine config / signed 64-bit session-token range'),
        'MAX_RESOURCE_BYTES': (
            '64 * 1024 * 1024', 'safety',
            'engine config / fixed per-resource file and tree-byte ceiling'),
        'MAX_RESOURCE_TREE_ENTRIES': (
            '4096', 'safety',
            'engine config / fixed bounded directory traversal; each file is one entry'),
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
        'TARGET_VERSION_MIN': (
            '1', 'format', 'lineage / accepted target-version range starts at version one'),
        'TARGET_VERSION_MAX': (
            '1073741824', 'format',
            'lineage / target version integer bound'),
        'TARGET_BRANCH_MAX': (
            '256', 'format',
            'lineage / target branch name character bound'),
        'TARGET_REF_MAX': (
            'len(TARGET_REF_PREFIX) + TARGET_BRANCH_MAX', 'format',
            'lineage / full refs/heads name length is prefix plus bounded branch name'),
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
        'MAXIMUM': (
            '1024 * 1024', 'safety',
            'model catalog / caps untrusted provider-command output at run(..., maximum=MAXIMUM)'),
        'OBSERVATION_BYTES': (
            '64 * 1024', 'safety',
            'model catalog / bounded observation text'),
        'OBSERVATION_COUNT': (
            '128', 'safety',
            'model catalog / bounded observation rows'),
    },
    'operator_records.py': {
        'MAX_OPERATOR_RECORD_BODY_CHARS': (
            '32768', 'safety',
            'operator records / fixed cap for user-authored immutable body content'),
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
        'ADAPTER_DIAGNOSTIC_BYTES_DEFAULT': (
            '2048', 'configured policy',
            'adapter failure diagnostics / default model-visible stderr tail; per-adapter setting overrides it under the 8192-byte ceiling'),
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
        'RECORD_NAME_MAX_CHARS': (
            '256', 'format',
            'shared records / maximum name length in the canonical record-name grammar'),
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
        'MAX_WAIT_RESOLUTION_TOKENS': (
            '200', 'mechanism',
            'snapshot progress / one token per row across the two public 100-row work-item pages'),
        '_PATH_VALIDATION_CACHE_LIMIT': (
            '256', 'safety',
            'snapshot / bounded in-process path-validation cache'),
        'MAX_MISSING_FINISH_STREAK': (
            '1073741824', 'format',
            'snapshot / persisted missing-finish streak counter ceiling'),
    },
    'storage_runs.py': {
        'MAX_ENTRIES': (
            'MAX_STORAGE_RUN_ENTRIES', 'safety',
            'storage runs / bounded scan entry ceiling re-exported from config'),
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
        'MAX_PROPOSAL_POLL_ERROR_CHARS': (
            '512', 'safety',
            'proposal polling / bounded surfaced adapter and recording diagnostics'),
    },
    'wait_events.py': {
        'MAX_ROW_BYTES': (
            '1024 * 1024', 'safety',
            'wait events / bounded row read'),
        'RECEIPT_BYTES': (
            '4096', 'safety',
            'wait events / bounded receipt read'),
    },
    'work_item_read_view.py': {},
    'work_item_seed.py': {
        'MAX_DECISION_TASKS': (
            '16', 'format',
            'work-item seed / fixed task count'),
    },
    'doctor.py': {
        '_CHECK_RUNNER_SETTINGS_MAXIMUM': (
            '65536', 'safety', 'doctor / bounded read of untrusted project settings before validation'),
    },
    'sandbox.py': {
        'CHECKPOINT_INSPECT_MAXIMUM': (
            '262144', 'safety',
            'checkpoint image inspection / bounded full output read so marker search refuses truncation'),
    },
    'scheduler_evidence.py': {
        'SPARSE_BELOW': (
            '5', 'mechanism',
            'scheduler evidence / diagnostic sparse-sample classification; never a scheduler policy input'),
    },
    'work_item_events.py': {
        'ACKED_ID_SAMPLE': (
            '10', 'safety',
            'work-item events / bounded recent acknowledged-ID sample while retaining the exact total'),
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
        'cache_seconds': ('DEFAULT_WEB_CACHE_SECONDS', 'configured policy',
                          'web / response-cache reuse and reclamation TTL default'),
        'search_feed_item_limit': ('200', 'configured policy', 'web / per-feed RSS/Atom candidate shaping'),
    },
    'vcs': {
        'artifact_full_read_chars': ('DEFAULT_ARTIFACT_FULL_READ_CHARS', 'configured policy',
                                     'VCS artifacts / whole-file character budget'),
        'poll_interval_seconds': ('DEFAULT_VCS_POLL_INTERVAL_SECONDS', 'configured policy',
                                  'VCS polling / cadence default shared with the defensive policy helper'),
        'poll_max_branches': ('4', 'configured policy', 'VCS polling / branches considered per daemon tick'),
        'poll_fetch_timeout_seconds': ('30', 'configured policy', 'VCS polling / upstream fetch timeout default'),
        'poll_status_timeout_seconds': ('15', 'configured policy', 'VCS polling / per-branch status timeout default'),
        'ci_rewake_seconds': ('1800', 'configured policy',
                              'VCS CI wake / unchanged-red re-wake interval default'),
    },
}
# Request-local retention only: cache misses repeat normal validation, so these
# memory-versus-I/O choices are policy, not fixed correctness/safety ceilings.
WORK_ITEM_READ_VIEW_CACHE = {
    'work_item_read_view_snapshot_cache_bytes': (
        '32 * 1024 * 1024', 'configured policy', 'Limits / request-local validated snapshot records'),
    'work_item_read_view_file_cache_bytes': (
        '8 * 1024 * 1024', 'configured policy', 'Limits / request-local file records'),
    'work_item_read_view_item_cache_bytes': (
        '8 * 1024 * 1024', 'configured policy', 'Limits / request-local parsed-item records'),
    'work_item_read_view_item_cache_entries': (
        '1024', 'configured policy', 'Limits / request-local parsed-item entries'),
}

CONFIGURED = {
    'Editor': {
        'search_max_bytes': ('8 * 1024 * 1024', 'configured policy', 'editor / per-request scan byte budget'),
        'search_max_matches': ('50', 'configured policy', 'editor / per-request match count'),
        'search_match_chars': ('1000', 'configured policy', 'editor / matching-line excerpt length'),
    },
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


PROJECT_GOAL_LIMIT = {
    'project_goal_bytes': (
        '65536', 'configured policy',
        'Limits / operator-selected UTF-8 project-goal ceiling with the previous 64 KiB default'),
}


# The physical-run retirement budget has its own Limits owner. MAX_ENTRIES in
# storage_runs remains the shared fixed safety ceiling, not a usage-report default.
RUN_RETENTION_SCAN = {
    'run_retention_scan_runs': ('5000', 'configured policy',
                                'Limits / physical-run retention scan work budget'),
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
# policy/default dictionaries above. Event-poll and whole-file artifact values
# are configured defaults; the Task Scheduler minimum is a mechanism floor.
# Include/role/resource/workspace ceilings are mechanism or safety; protocol
# integers and wait-seconds are format bounds. The inventory test adds names
# already declared by the special dictionaries, so a new unexplained literal
# fails it.
CONFIG_MODULE_BOUNDS = {
    'MAX_EDITOR_SEARCH_MATCH_CHARS': (
        'READ_PAGE_CHARS', 'safety', 'editor / fixed maximum excerpt uses the shared read-page envelope'),
    'MAX_INSIGHT_HISTORY_ENTRIES': (
        '1000', 'safety', 'insights / fixed ceiling for explicit history output rows'),
    'DEFAULT_INSIGHT_HISTORY_ENTRIES': (
        '1000', 'configured policy',
        'insights / default for [limits].insight_history_entries'),
    'DEFAULT_ARTIFACT_FULL_READ_CHARS': (
        '524288', 'configured policy', 'VCS artifacts / default whole-file character budget'),
    'MAX_STORAGE_RUN_ENTRIES': (
        '100000', 'safety', 'config / aggregate per-project run-reference scan entry ceiling'),
    'MAX_PROJECT_GOAL_BYTES': (
        '1073741824', 'safety',
        'config / absolute project-goal input ceiling; snapshot audit uses it for older configurations'),
    'MAX_CHECK_INPUT_BOUND': (
        '1 << 40', 'safety',
        'config / one shared absolute ceiling for check-input traversal bounds in settings, jobs and receipts'),
    'MEBIBYTE_BYTES': (
        '1024 * 1024', 'mechanism',
        'config / exact binary unit conversion for byte budgets named in MiB'),
    'DEFAULT_WEB_CACHE_SECONDS': (
        '1800', 'configured policy',
        'web / default for [web].cache_seconds; explicit project settings override'),
    'DEFAULT_PROBE_EXECUTABLE_BYTES': (
        '256 * 1024 * 1024', 'configured policy',
        'recovery probes / default for [limits].probe_executable_bytes; prior host I/O refusal bound'),
    'MAX_USAGE_POLICY_COUNT': (
        'min(TOML_INTEGER_MAX, sys.maxsize)', 'safety',
        'config / report counts fit the TOML integer and host collection-index ranges'),
    'MAX_USAGE_RUNS_SCANNED': (
        'MAX_USAGE_POLICY_COUNT - 1', 'safety',
        'usage / reserves the extra candidate index that detects an incomplete history window'),
    'MAX_DISPATCH_EVIDENCE_COUNT': (
        'min(TOML_INTEGER_MAX, sys.maxsize)', 'safety',
        'dispatch evidence / counts fit TOML integers and host collection indexes'),
    'MAX_DISPATCH_EVIDENCE_BYTES': (
        'min(TOML_INTEGER_MAX, sys.maxsize - 1)', 'safety',
        'dispatch evidence / reserves a representable byte for safe_read growth detection'),
    'MIN_DISPATCH_EVIDENCE_BYTES': (
        "len(canonical({'schema': 1, 'dropped_attempts': TOML_INTEGER_MAX, 'attempts': []}))",
        'format', 'dispatch evidence / minimum serialized empty schema-1 envelope'),
    'MAX_RESOURCES': (
        '64', 'safety', 'config / bounded per-profile local resource list'),
    'DEFAULT_CI_CACHE_MAX_BRANCHES': (
        '128', 'configured policy', 'config / unchanged vcs.ci_cache_max_branches default'),
    'DEFAULT_CI_CACHE_MAX_CHECKS_PER_BRANCH': (
        '16', 'configured policy', 'config / unchanged vcs.ci_cache_max_checks_per_branch default'),
    'MAX_CI_CACHE_ROWS': (
        '2048', 'safety', 'config / fixed total rows per CI observation mapping, not a provider bound'),
    'MAX_CI_WAKE_EXTRA_BRANCHES': (
        '64', 'mechanism', 'config / fixed CI wake branch-set bound'),
    'MAX_WAIT_SECONDS': (
        '86400', 'format', 'protocol / finish wait_seconds wire bound; config also reuses it as the lock-wait policy envelope'),
    'MAX_NO_PROGRESS_BACKOFF_SECONDS': (
        'MAX_WAIT_SECONDS', 'safety',
        'config / shared upper bound for no-progress timers and their bounded snapshot diagnostic'),
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
    'MAX_SYSTEMD_VERIFY_TIMEOUT_SECONDS': (
        '2147483', 'mechanism',
        'services / largest whole-second subprocess selector timeout fitting signed 32-bit milliseconds'),
    'EVENT_POLL_SECONDS_DEFAULT': (
        '60', 'configured policy', 'service events / unchanged per-role recovery-poll default'),
    'EVENT_POLL_SECONDS_MIN': (
        '60', 'mechanism', 'service events / Task Scheduler minimum poll interval'),
    'EVENT_POLL_SECONDS_MAX': (
        '31 * 86400', 'safety', 'config / event-poll interval validation maximum (31 days)'),
    'MAX_WORKSPACE_CACHE_DEPTH': (
        '64', 'safety', 'workspace cache / traversal depth ceiling; configured max_depth may lower it'),
    'DOCTOR_TIMEOUT_MAX_SECONDS': (
        '86400', 'safety', 'doctor / probe deadline safety ceiling; configured values may lower it'),
}


CALL_KEYWORDS = frozenset({
    'timeout', 'timeout_seconds', 'maximum', 'max_bytes', 'max_files',
    'max_entries', 'max_depth', 'limit', 'maxsize', 'length',
})
# (enclosing scope, callee, keyword or bounded-maximum arg, occurrence):
#     (expression, class, rationale)
CALLS = {
    'classification.py': {
        ('_model_summary', 'bounded', 'maximum', 0): (
            'MAX_CLASSIFICATION_MODEL_SUMMARY_BYTES', 'safety',
            'classification / cap the normalized model summary before canonical encoding'),
        ('classify', 'bounded', 'maximum', 0): (
            'MAX_CLASSIFICATION_CACHE_BYTES', 'safety',
            'classification / cap the cached result envelope before persistence'),
    },
    'probes.py': {
        ('_service', 'bounded', 'maximum', 0): (
            '4096', 'safety', 'recovery probes / cap the shared per-event JSON evidence record'),
    },
    'selection.py': {
        ('attributes', 'bounded', 'maximum', 0): (
            '16384', 'safety', 'selection / cap canonical classifier attributes before retaining them'),
        ('validate_condition', 'bounded', 'maximum', 0): (
            '16384', 'safety', 'selection / cap each canonical condition before recursive validation'),
        ('validate_event', 'bounded', 'maximum', 0): (
            '4096', 'safety', 'selection / cap one persisted probe event envelope'),
        ('work_inputs', 'bounded', 'maximum', 0): (
            'MAX_WORK_BYTES', 'safety', 'selection / cap assembled classifier inputs before returning the work document'),
        ('select', 'bounded', 'maximum', 0): (
            '262144', 'safety', 'selection / cap assembled decision evidence before returning it'),
        ('write_wait', 'bounded', 'maximum', 0): (
            '262144', 'safety', 'selection / cap one saved wait-coalescing record before writing'),
    },
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
    'decision_delivery.py': {
        ('read', 'store(context).read_page', 'limit', 0): (
            "args.get('limit', 8192)", 'safety',
            'decision delivery / omitted limit defaults to the shared-record page ceiling'),
    },
    'insight_tools.py': {
        ('insights', 'page', 'maximum', 0): (
            'PREVIEW_BYTES * 2', 'safety',
            'insight tools / bounded result page for two preview-sized bodies'),
    },
    'process.py': {
        ('_stop_io', 'thread.join', 'timeout', 0): (
            'max(0.0, deadline - time.monotonic())', 'safety',
            'process I/O stop / nonnegative remaining time under the shared stop deadline'),
        ('send_bounded', 'thread.join', 'timeout', 0): (
            'min(0.01, max(0, deadline - time.monotonic()))', 'mechanism',
            'bounded RPC send / 10-ms cancellation poll clipped to the caller deadline'),
    },
    'runtime.py': {
        ('_has_ready_work', 'project.work_items.page_ready', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row readiness existence check'),
        ('Context._op_vcs_read_publications', 'Publication.list_receipts', 'limit', 0): (
            '100', 'mechanism', 'runtime / publication receipt public page bound'),
        ('Context._op_vcs_read_ci_requests',
         'CiRequests(self.project.root, self.config.ci_requests).daily', 'limit', 0): (
            '100', 'mechanism', 'runtime / CI-request public page bound'),
        ('Context._op_check_job', 'check_jobs.logs', 'limit', 0): (
            "args.get('limit', 8192)", 'safety',
            'runtime / check-log page defaults to the bounded reader ceiling'),
        ('Context._op_vcs_read_ci_costs',
         'CiRequests(self.project.root, self.config.ci_requests).page', 'limit', 0): (
            "args.get('limit', 8)", 'policy',
            'runtime / caller-selected CI-cost dispatch page default'),
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
        ('_role_events_due', 'project.insights.due_waits', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row due-wait existence check'),
        ('_role_events_due', '_receipt_wait_due', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row combined receipt-wait check'),
        ('should_run', '_receipt_wait_due', 'limit', 0): (
            '1', 'mechanism', 'runtime / one-row combined receipt-wait scheduling check'),
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
    'snapshot.py': {
        ('_exclude_matcher', 'lru_cache', 'maxsize', 0): (
            '16', 'mechanism',
            'snapshot / bounded process-local matcher cache; eviction only recompiles'),
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
        ('_cmd_insight', 'project.insights.list_page', 'limit', 0): (
            'args.limit if args.limit is not None else 30', 'policy',
            'CLI / insight page default; callers may select --limit'),
        ('_cmd_work_items', 'store.page_open_readonly', 'limit', 0): (
            'args.limit if args.limit is not None else 25', 'policy',
            'CLI / open work-item page default; callers may select --limit'),
        ('_cmd_work_items', 'closed_page', 'limit', 0): (
            'args.closed if args.closed is not None else 10', 'policy',
            'CLI / closed work-item page default; callers may select --closed'),
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
        ('Channel.__init__', 'queue.Queue', 'maxsize', 0): (
            '128', 'mechanism', 'engine channel / fixed parsed-record backpressure queue depth'),
    },
    'engine_config.py': {
        ('stop_engine_containers', 'run', 'maximum', 0): (
            '8192', 'safety', 'engine config / bounded cleanup output read'),
    },
    'operator_records.py': {
        ('store', 'KindPolicy', 'maximum', 0): (
            '140000', 'safety', 'operator records / bounded shared-record payload envelope'),
        ('read', 'store(context.project).read_page', 'limit', 0): (
            "args.get('limit', 8192)", 'safety',
            'operator records / omitted limit defaults to the shared-record page ceiling'),
    },
    'persistent_exec.py': {
        ('ExecSession._remove', 'run', 'maximum', 0): (
            '8192', 'safety', 'persistent exec / bounded cleanup output read'),
    },
    'pi_reselection.py': {
        ('_family_record', 'read_json', 'max_bytes', 0): (
            '4096', 'safety', 'pi reselection / bounded family record read'),
    },
    'platform.py': {
        ('terminate_process', 'process.wait', 'timeout', 0): (
            '0.2 if grace == 0 else 5', 'safety',
            'platform / bounded post-kill reap wait, shortened for immediate cancellation'),
        ('terminate_process', 'process.wait', 'timeout', 1): (
            '0.2 if grace == 0 else 5', 'safety',
            'platform / bounded Windows job-close reap wait'),
        ('terminate_process', 'process.wait', 'timeout', 2): (
            '2', 'safety', 'platform / bounded process grace wait before kill fallback'),
        ('terminate_process', 'process.wait', 'timeout', 3): (
            '0.2 if grace == 0 else 5', 'safety',
            'platform / bounded post-kill reap wait after portable termination'),
    },
    'proposal_waits.py': {
        ('shared_store', 'KindPolicy', 'maximum', 0): (
            '8192', 'safety', 'proposal waits / bounded shared-record payload envelope'),
    },
    'safe_point.py': {
        ('read', 'read_json', 'max_bytes', 0): (
            '4096', 'safety', 'safe point / bounded saved-state read'),
    },
    'storage.py': {
        ('_compress_event_log', 'shutil.copyfileobj', 'length', 0): (
            '1048576', 'safety', 'storage / 1-MiB copy chunk bounds transient compression input'),
    },
    'web_operations.py': {
        ('search', 'page', 'maximum', 0): (
            'PREVIEW_BYTES * 2', 'safety',
            'web operations / bounded result page for two preview-sized bodies'),
    },
    'work_item_operations.py': {
        ('read', 'context.project.work_items.explain', 'limit', 0): (
            "args.get('limit', 25)", 'policy',
            'work items / by-ID history page default; caller may select limit'),
        ('read', 'context.project.work_items.page_open', 'limit', 0): (
            "args.get('limit', 25)", 'policy',
            'work items / open-page default; caller may select limit'),
        ('legacy_read', 'context.project.work_items.page_legacy', 'limit', 0): (
            "args.get('limit', 25)", 'policy',
            'work items / legacy-page default; caller may select limit'),
    },
    'work_item_read_view.py': {
        ('readonly_view', 'lru_cache', 'maxsize', 0): (
            '8192', 'safety',
            'work items / bounded request-local path-key cache, eviction revalidates'),
    },
    'sandbox.py': {
        ('remove_leftover', 'run', 'maximum', 0): (
            '8192', 'safety', 'sandbox / bounded leftover-container list output'),
        ('remove_leftover', 'run', 'maximum', 1): (
            '8192', 'safety', 'sandbox / bounded leftover-container removal output'),
        ('Sandbox.execute', 'run', 'maximum', 0): (
            '8192', 'safety', 'sandbox / bounded cleanup output read'),
        ('_remove_containers', 'run', 'maximum', 0): (
            '65536', 'safety', 'sandbox / bounded project-container list output'),
        ('_remove_containers', 'run', 'maximum', 1): (
            '65536', 'safety', 'sandbox / bounded bulk removal output'),
    },
}


# Whole underscore-delimited words; avoid unrelated names such as "discount".
PARAMETER_BOUND = re.compile(
    r'(?:^|_)(?:limits?|max(?:imum)?|caps?|timeouts?|seconds?|days?|bytes?|size|'
    r'depth|counts?|entries|rows?|offset|retries|budget|retention|threshold)(?:_|$)'
)
# (relative file, qualified class/function scope, parameter):
#     (exact ast.unparse(default), classification, owner / rationale)
PARAMETER_DEFAULTS = {
    ('budget.py', 'Budget.__init__', 'retention_days'): (
        '31', 'configured policy', 'budget / [limits] retention_days fallback'),
    ('check_jobs.py', 'logs', 'offset'): (
        '0', 'mechanism', 'check jobs / initial evidence page position'),
    ('check_jobs.py', 'logs', 'limit'): (
        '8192', 'policy', 'check jobs / caller-selected evidence page length'),
    ('ci_requests.py', 'CiRequests.daily', 'limit'): (
        '100', 'policy', 'CI requests / caller-selected daily request projection size'),
    ('ci_requests.py', 'CiRequests.page', 'limit'): (
        '8', 'policy', 'CI requests / caller-selected cost-view dispatch page size'),
    ('ci_requests.py', 'CiRequests.page', 'offset'): (
        '0', 'mechanism', 'CI requests / initial dispatch page position'),
    ('ci_waits.py', 'pending', 'limit'): (
        '10', 'configured policy', 'CI waits / project [limits].wait_events_limit per-source delivery batch'),
    ('ci_waits.py', 'readiness_init', 'limit'): (
        '10', 'policy', 'CI waits / caller-selected readiness candidate batch'),
    ('ci_waits.py', '_shared_page', 'limit'): (
        '10', 'configured policy', 'CI waits / project [limits].wait_events_limit per-source delivery batch'),
    ('ci_waits.py', 'delivery', 'limit'): (
        '10', 'configured policy', 'CI waits / project [limits].wait_events_limit per-source delivery batch'),
    ('fs.py', 'page', 'offset'): (
        '0', 'mechanism', 'fs / initial list projection position'),
    ('fs.py', 'page', 'limit'): (
        '1000', 'policy', 'fs / caller-selected list projection size'),
    ('fs.py', 'text_preview', 'maximum'): (
        '8192', 'safety', 'fs / fallback bounded text preview, callers may narrow or override'),
    ('insights.py', 'Insights.__init__', 'retention_days'): (
        '31', 'configured policy', 'insights / Project supplies [limits] retention_days'),
    ('insights.py', 'Insights.__init__', 'cursor_ttl_seconds'): (
        '86400', 'configured policy', 'insights / Project supplies [limits] insight_cursor_ttl_seconds'),
    ('insights.py', 'Insights.__init__', 'history_entries'): (
        '1000', 'configured policy',
        'insights / Project supplies [limits].insight_history_entries'),
    ('insights.py', 'Insights.projection', 'limit'): (
        '30', 'policy', 'insights / caller-selected live projection size'),
    ('insights.py', 'Insights.list', 'limit'): (
        '30', 'policy', 'insights / caller-selected live list size'),
    ('insights.py', 'Insights.list_page', 'limit'): (
        '30', 'policy', 'insights / caller-selected live/archive cursor page size'),
    ('insights.py', 'Insights.due_waits', 'limit'): (
        '10', 'configured policy', 'insights / project [limits].wait_events_limit per-source due-wait delivery batch'),
    ('insights.py', 'Insights.pending_obligations', 'limit'): (
        '10', 'configured policy', 'insights / project [limits].wait_events_limit per-source obligation delivery batch'),
    ('insights.py', 'Insights.decision_events', 'limit'): (
        '10', 'policy', 'insights / caller-selected decision-event batch'),
    ('insights.py', 'Insights.pending_insight_events', 'limit'): (
        '10', 'policy', 'insights / caller-selected pending insight-event batch'),
    ('insights.py', 'Insights.shared_decision_page', 'limit'): (
        '10', 'policy', 'insights / caller-selected shared decision page size'),
    ('insights.py', 'Insights.decision_events_with_diagnostics', 'limit'): (
        '10', 'policy', 'insights / caller-selected decision-event diagnostic projection size'),
    ('pi_durable_store.py', 'project_context', 'max_bytes'): (
        '65536', 'policy', 'Pi durable store / caller-selected summary truncation marker threshold, not a read cap'),
    ('proposal_waits.py', 'delivery', 'limit'): (
        '10', 'configured policy', 'proposal waits / project [limits].wait_events_limit per-source delivery batch'),
    ('proposal_waits.py', 'pending', 'limit'): (
        '10', 'configured policy', 'proposal waits / project [limits].wait_events_limit per-source delivery batch'),
    ('proposal_waits.py', 'readiness_init', 'limit'): (
        '10', 'policy', 'proposal waits / caller-selected readiness candidate batch'),
    ('protocol.py', 'text', 'maximum'): (
        '32768', 'safety', 'protocol / default schema string validation ceiling'),
    ('protocol.py', 'array', 'maximum'): (
        '30', 'safety', 'protocol / default schema array validation ceiling'),
    ('publication.py', 'Publication.list_receipts', 'limit'): (
        '100', 'policy', 'publication / caller-selected receipt listing size'),
    ('runtime.py', '_receipt_wait_due', 'limit'): (
        '1', 'mechanism', 'runtime / existence probe needs one ready candidate per route'),
    ('selection.py', 'validate_condition', 'depth'): (
        '0', 'mechanism', 'selection / root nesting counter, not the nesting ceiling'),
    ('shared_records.py', '_json', 'depth'): (
        '0', 'mechanism', 'shared records / root JSON nesting counter, not the nesting ceiling'),
    ('shared_records.py', 'SharedRecords.read_page', 'offset'): (
        '0', 'mechanism', 'shared records / initial envelope character position'),
    ('snapshot.py', 'Snapshots.history', 'limit'): (
        '8', 'policy', 'snapshots / caller-selected anchored history projection size'),
    ('storage.py', 'restore', 'max_bytes'): (
        '1073741824', 'configured policy', 'storage / CLI restore --max-bytes fallback'),
    ('storage_runs.py', 'Scan.preflight', 'depth'): (
        '0', 'mechanism', 'run storage / root traversal depth counter, not the depth ceiling'),
    ('vcs.py', 'parse_artifact', 'offset'): (
        '0', 'mechanism', 'VCS / initial artifact byte position; whole-file mode requires zero'),
    ('work_item_events.py', 'pending', 'limit'): (
        '10', 'policy', 'work-item events / caller-selected terminal delivery batch'),
    ('work_item_events.py', 'progress', 'limit'): (
        '100', 'policy', 'work-item events / caller-selected role summary count'),
    ('work_item_events.py', 'closed_page', 'limit'): (
        '10', 'policy', 'work-item events / caller-selected closed-item delivery page size'),
    ('work_items.py', 'WorkItems.__init__', 'doing_limit'): (
        '1', 'configured policy', 'work items / Project supplies [limits] work_items_doing'),
    ('work_items.py', 'WorkItems.__init__', 'stale_seconds'): (
        '86400', 'configured policy', 'work items / Project supplies [limits] work_items_stale_seconds'),
    ('work_items.py', 'WorkItems.__init__', 'blocked_max_age_seconds'): (
        '604800', 'configured policy',
        'work items / Project supplies [limits] work_items_blocked_max_age_seconds'),
    ('work_items.py', 'WorkItems.page_legacy', 'limit'): (
        '25', 'policy', 'work items / caller-selected legacy listing size'),
    ('work_items.py', 'WorkItems.explain', 'limit'): (
        '25', 'policy', 'work items / caller-selected explanation history size'),
    ('work_items.py', 'WorkItems.page_open', 'limit'): (
        '25', 'policy', 'work items / caller-selected open-item page size'),
    ('work_items.py', 'WorkItems.page_open_for_order', 'limit'): (
        '25', 'policy', 'work items / caller-selected work-order open-item page size'),
    ('work_items.py', 'WorkItems.page_ready', 'limit'): (
        '25', 'policy', 'work items / caller-selected ready-item page size'),
    ('work_items.py', 'WorkItems.page_open_readonly', 'limit'): (
        '25', 'policy', 'work items / caller-selected read-only open-item page size'),
    ('work_items.py', 'WorkItems.blocked_liveness_readonly', 'limit'): (
        '25', 'policy', 'work items / caller-selected blocked-liveness diagnostic sample'),
    ('work_items.py', 'WorkItems.open_state_readonly', 'limit'): (
        '25', 'policy', 'work items / caller-selected open-state projection size'),
    ('work_items.py', 'WorkItems.page_closed_readonly', 'limit'): (
        '10', 'policy', 'work items / caller-selected read-only closed-item page size'),
}


# (relative file, qualified scope, callee, positional argument, occurrence):
#     (exact ast.unparse(argument), classification, owner / rationale)
# These are wait/backoff intervals: no format or user payload is involved.
TIMED_WAIT_ARGUMENTS = {
    ('platform.py', 'lock_fd', 'time.sleep', 0, 0): (
        'min(retry_interval_ms / 1000, remaining)', 'configured policy',
        'platform / [limits].lock_retry_interval_ms sets kernel-probe cadence; clip it to the acquisition deadline'),
    ('platform.py', 'lock_fd', 'time.sleep', 0, 1): (
        'min(retry_interval_ms / 1000, remaining)', 'configured policy',
        'platform / [limits].lock_retry_interval_ms sets kernel-probe cadence; clip it to the acquisition deadline'),
    ('process.py', '_input_thread.write', 'stop.wait', 0, 0): (
        '0.01', 'mechanism',
        'process input / wait after a nonblocking-write stall to avoid spinning while retaining prompt cancellation'),
    ('process.py', 'run.pump', 'stop.wait', 0, 0): (
        '0.01', 'mechanism',
        'process output / wait after a pipe-readiness race to avoid spinning while retaining prompt cancellation'),
    ('process.py', 'run', 'changed.wait', 0, 0): (
        '0.05 if deadline is None else min(0.05, max(0, deadline - time.monotonic()))',
        'mechanism',
        'process output / bounded change wait, clipped to the command deadline and used only as an event-wait fallback'),
    ('engine_channel.py', 'Channel._read', 'self.stop.wait', 0, 0): (
        '0.01', 'mechanism',
        'engine channel / wait after a pipe-readiness race to avoid spinning while retaining prompt cancellation'),
    ('persistent_exec.py', 'ExecSession._start.pump', 'self.stop.wait', 0, 0): (
        '0.01', 'mechanism',
        'persistent exec / wait after a pipe-readiness race to avoid spinning while retaining prompt cancellation'),
    ('persistent_exec.py', 'ExecSession.execute', 'self.changed.wait', 0, 0): (
        '0.02', 'mechanism',
        'persistent exec / bounded event-change fallback while consuming command output'),
}


# (relative file, qualified scope, positional argument, occurrence):
#     (exact ast.unparse(argument), classification, owner / rationale)
RANGE_ARGUMENTS = {
    ('check_jobs.py', 'submit.request_slot', 0, 0): (
        '1025', 'safety',
        'check jobs / identity search is one candidate beyond MAX_CHECK_RETAINED'),
    ('decision_delivery.py', 'pending', 1, 0): (
        '-1', 'mechanism', 'decision delivery / reverse-range sentinel'),
    ('decision_delivery.py', 'pending', 2, 0): (
        '-1', 'mechanism', 'decision delivery / reverse-range step'),
    ('fs.py', '_safe_write_openat', 0, 0): (
        '8', 'safety',
        'fs / bounded UUID temporary-name collision attempts; fail closed if all eight collide'),
    ('scheduler_evidence.py', 'summarize', 0, 0): (
        '1', 'mechanism', 'scheduler evidence / begin comparison at the second entry'),
    ('scheduler_evidence.py', 'summarize', 0, 1): (
        '0', 'mechanism', 'scheduler evidence / begin disjoint adjacent-pair grouping at the first entry'),
    ('scheduler_evidence.py', 'summarize', 2, 0): (
        '2', 'mechanism', 'scheduler evidence / adjacent-pair stride'),
    ('snapshot.py', 'Snapshots.merge', 0, 0): (
        '1', 'mechanism', 'snapshot merge / begin path-component traversal after the root'),
    ('snapshot.py', 'Snapshots.validate_paths', 0, 0): (
        '1', 'mechanism', 'snapshot paths / begin ancestor checks after the root component'),
    ('snapshot_manifest.py', '_goal', 0, 0): (
        '3', 'format',
        'snapshot manifest / maximum UTF-8 continuation bytes after a leading byte'),
    ('web.py', 'Web.fetch', 0, 0): (
        '4', 'safety',
        'web fetch / fixed redirect-hop ceiling; every hop revalidates the destination'),
    ('work_items.py', 'WorkItems._sidecar_expected_rev', 0, 0): (
        '2', 'format', 'work items / revision files begin after immutable seed revision 1'),
    ('work_items.py', 'WorkItems._iter_chain', 0, 0): (
        '2', 'format', 'work items / revision files begin after immutable seed revision 1'),
    ('work_items.py', 'WorkItems._iter_chain', 0, 1): (
        '2', 'format', 'work items / replay revision files begin after immutable seed revision 1'),
}

# (relative file, qualified scope, positional argument, occurrence):
#     (exact ast.unparse(argument), classification, owner / rationale)
SYMBOLIC_RANGE_ARGUMENTS = {
    ('admission.py', 'check_slot_exclusions', 0, 0): (
        'max(0, slots)', 'configured policy',
        'admission / one exclusion per configured parallel_checks slot'),
    ('admission.py', '_Queue._enqueue', 0, 0): (
        'self.capacity', 'configured policy',
        'admission / pending-owner search bounded by limits.admission_queue_capacity'),
    ('admission.py', '_Queue._slots', 0, 0): (
        'self.slots', 'configured policy',
        'admission / active-slot search bounded by limits.parallel_runs'),
    ('check_jobs.py', '_run_one', 0, 0): (
        'project.config.limits.parallel_checks', 'configured policy',
        'check jobs / compute-slot search bounded by limits.parallel_checks'),
    ('check_jobs.py', 'run_pending', 0, 0): (
        'project.config.limits.check_pending', 'configured policy',
        'check jobs / drain batch bounded by limits.check_pending'),
    ('check_jobs.py', 'recover', 0, 0): (
        'project.config.limits.parallel_checks', 'configured policy',
        'check jobs / fault-slot search bounded by limits.parallel_checks'),
    ('ci_acceptance.py', 'policy', 0, 0): (
        'len(commands)', 'mechanism',
        'CI acceptance / exact coverage check iterates each bounded command index'),
    ('ci_polling.py', 'poll_ci', 0, 0): (
        'max_branches', 'configured policy',
        'CI polling / rotating branch batch bounded by vcs.poll_max_branches'),
    ('decision_delivery.py', 'pending', 0, 0): (
        'len(delivered) - 1', 'mechanism',
        'decision delivery / reverse scan of the bounded page trims optional attention rows'),
    ('scheduler_evidence.py', 'summarize', 1, 0): (
        'len(sequence)', 'mechanism',
        'scheduler evidence / overlap scan walks the already-bounded sorted sequence'),
    ('scheduler_evidence.py', 'summarize', 1, 1): (
        'len(sequence) - 1', 'mechanism',
        'scheduler evidence / adjacent-pair scan walks the already-bounded sorted sequence'),
    ('snapshot.py', 'Snapshots.merge', 0, 0): (
        'self.history_index', 'configured policy',
        'snapshot merge / ancestry walk bounded by limits.history_index parent edges'),
    ('snapshot.py', 'Snapshots.merge', 1, 0): (
        'len(parts)', 'mechanism',
        'snapshot merge / prefix-conflict check walks each validated path component'),
    ('snapshot.py', 'Snapshots.validate_paths', 1, 0): (
        "len(name.split('/'))", 'mechanism',
        'snapshot paths / ancestor-conflict check walks each validated path component'),
    ('storage.py', '_snapshot_ancestry', 0, 0): (
        'history_index', 'configured policy',
        'storage retention / merge ancestry protection uses limits.history_index edges'),
    ('work_item_seed.py', 'task_seeds', 0, 0): (
        'len(tasks)', 'mechanism',
        'work-item task manifest / visit every validated member within the 16-task contract'),
    ('work_items.py', 'WorkItems._sidecar_expected_rev', 1, 0): (
        'len(numeric) + 2', 'format',
        'work items / verify revision filenames form a contiguous seed-relative chain'),
    ('work_items.py', 'WorkItems._iter_chain', 1, 0): (
        'len(revisions) + 2', 'format',
        'work items / validate contiguous revision numbers after immutable seed revision 1'),
    ('work_items.py', 'WorkItems._iter_chain', 1, 1): (
        'len(revisions) + 2', 'format',
        'work items / replay every validated revision within MAX_REVISIONS'),
}

# Direct numeric comparison operands with magnitude >= 3 in selected modules.
# Numeric-only basic arithmetic is evaluated; no source is executed.
# (file, qualified scope, operand index, occurrence):
#     (exact ast.unparse(operand), classification, owner / rationale)
COMPARISON_BOUND_ARGUMENTS = {
    ('bridge.py', 'connect', 0, 0): (
        '1', 'format',
        'TCP bridge / port zero is a bind-time ephemeral sentinel, not a valid dial target'),
    ('bridge.py', 'connect', 2, 0): (
        '65535', 'format', 'TCP bridge / maximum value of the 16-bit port field'),
    ('ci_observation.py', '_valid_ci_run', 1, 0): (
        '128', 'safety',
        'CI observation / bound the opaque cached run identity before persistence and projection'),
    ('claude.py', 'model_delta', 3, 0): (
        '2 ** 63 - 1', 'format',
        'Claude cumulative token usage / cap each counter at the signed 64-bit usage-field maximum'),
    ('claude.py', 'validated_api_error_status', 0, 0): (
        '100', 'format', 'Claude SDK error evidence / HTTP status codes begin at 100'),
    ('claude.py', 'validated_api_error_status', 2, 0): (
        '599', 'format', 'Claude SDK error evidence / HTTP status codes end at 599'),
    ('continuations.py', '_aware_time', 1, 0): (
        '32', 'safety',
        'continuation state / bound persisted timestamp text before ISO-8601 parsing'),
    ('delegation_jobs.py', 'DelegationJobs._validate', 1, 0): (
        '4', 'format',
        'delegation ledger / require the exact persisted schema version before interpreting state'),
    ('errors.py', 'ModelFailure.__init__', 1, 0): (
        '16384', 'safety',
        'model failure / cap canonical adapter details before retaining error evidence'),
    ('errors.py', 'ModelFailure.__init__', 2, 0): (
        '253402300799', 'format',
        'model failure / retry timestamps stop at the Unix-second ceiling for year 9999'),
    ('explain.py', '_control', 1, 0): (
        '128', 'safety',
        'explain control / cap wake-generation text in the bounded status projection'),
    ('explain.py', '_control', 1, 1): (
        '512', 'safety',
        'explain control / cap operator-authored reason text in the status projection'),
    ('ci_observation.py', '_clean_ci_failures', 1, 0): (
        '256', 'format',
        'CI observation / check-name length follows the shared 1-256 character VCS schema'),
    ('cli.py', '_parse_wait', 1, 0): (
        '4', 'format',
        'CLI dependency wait / exactly four colon-delimited recipient, insight, revision '
        'and action fields'),
    ('codex.py', 'CodexDriver.execute.request', 1, 0): (
        '128', 'safety',
        'Codex app-server initialize / cap unmatched notification records retained before the response'),
    ('config.py', '_vcs_prefix', 1, 0): (
        '256', 'format',
        'VCS configuration / branch-prefix length follows the shared 256-character branch-name contract'),
    ('config.py', '_include_target', 1, 0): (
        '4096', 'mechanism',
        'configuration include / fixed per-entry path bound in the ADR-006 include contract'),
    ('config.py', 'role_decision_events_limit', 2, 0): (
        '100', 'mechanism',
        'role policy / effective batch must honor the documented fixed 100-event delivery ceiling'),
    ('config.py', 'role_work_item_events_limit', 2, 0): (
        '100', 'mechanism',
        'role policy / effective batch must honor the documented fixed 100-event delivery ceiling'),
    ('config.py', 'role_no_progress_backoff_seconds', 2, 0): (
        '86400', 'configured policy',
        'role policy / effective timer override follows the [limits] 0–86,400-second range'),
    ('config.py', 'role_cooldown_seconds', 2, 0): (
        '1073741824', 'configured policy',
        'role policy / effective cooldown override follows the [limits] configured range'),
    ('config.py', 'role_daemon_spacing_seconds', 2, 0): (
        '1073741824', 'configured policy',
        'role policy / explicit daemon spacing follows the configured one-to-1073741824-second range'),
    ('config.py', 'workspace_cache_policy', 1, 0): (
        '32', 'safety',
        'workspace cache / bound the root list and its pairwise overlap validation'),
    ('config.py', 'workspace_cache_policy', 1, 1): (
        '1024', 'safety',
        'workspace cache / cap each UTF-8 relative root path before path processing'),
    ('config.py', 'workspace_cache_policy', 1, 2): (
        '32', 'safety',
        'workspace cache / cap root path components before filesystem processing'),
    ('config.py', 'load', 2, 0): (
        '1000000', 'configured policy',
        'limits / session_max_cost_usd is an operator-selected session-rotation threshold, not a bill'),
    ('config.py', 'load', 1, 0): (
        '32', 'safety',
        'sandbox / bound runner argv count passed into the configured container'),
    ('config.py', 'load', 1, 1): (
        '4096', 'safety',
        'sandbox / bound each configured runner argv string before process construction'),
    ('config.py', 'load', 1, 2): (
        '262144', 'safety',
        'profiles / cap canonical engine options and MCP configuration to 256 KiB'),
    ('config.py', 'load', 1, 3): (
        '64', 'safety',
        'profiles / bound explicit MCP server configuration count'),
    ('config.py', 'load', 1, 4): (
        '128', 'safety',
        'roles / bound the explicit native engine-tool grant list'),
    ('config.py', 'load', 1, 5): (
        '256', 'safety',
        'roles / bound each configured native engine-tool name'),
    ('config.py', 'load', 1, 6): (
        '8', 'safety',
        'roles / bound the subscribed decision-action list'),
    ('config.py', 'load', 1, 7): (
        '256', 'format',
        'VCS configuration / CI branch length follows the shared 256-character branch-name contract'),
    ('config.py', 'load', 1, 8): (
        '256', 'format',
        'VCS configuration / protected-ref length follows the shared 256-character branch-name contract'),
    ('persistent_exec.py', 'ExecSession.execute', 1, 0): (
        '255', 'format',
        'persistent exec / POSIX shell completion status fits the 0-255 status range'),
    ('persistent_exec.py', 'ExecSession.execute', 1, 1): (
        '4', 'format',
        'persistent exec / completion trailer is at most three decimal digits plus US'),
    ('runtime.py', 'Context._handle', 1, 0): (
        '65536', 'safety',
        'runtime usage / fixed canonical bridge-frame byte ceiling for one observation'),
    ('runtime.py', 'Context._handle', 1, 1): (
        '4096', 'safety',
        'runtime usage / fixed per-run retained observation-row ceiling'),
    ('runtime.py', 'Context._covered_waits', 1, 0): (
        '16', 'format',
        'VCS request / covered work-item count follows the public request protocol'),
    ('runtime.py', 'Context._op_finish', 1, 0): (
        '512', 'format',
        'finish / acknowledgement-token count follows the public maxItems contract'),
    ('runtime.py', 'Context._op_finish', 1, 1): (
        '64', 'format',
        'finish / acknowledgement-token length follows the public maxLength contract'),
    ('runtime.py', '_producer_limit', 2, 0): (
        '1073741824', 'configured policy',
        'producer holds / open_work_items and producer_inputs_per_source use the configured range'),
    ('runtime.py', '_declared_next', 1, 0): (
        '32000', 'format',
        'writer handoff / state text follows the public finish schema length'),
    ('runtime.py', '_declared_next', 1, 1): (
        '4096', 'format',
        'writer handoff / actionable Next step follows the documented declaration format bound'),
    ('admission.py', '_valid_resource', 1, 0): (
        '3', 'format',
        'admission resource key / project, locks directory and lock filename are exactly three path components'),
    ('admission.py', '_valid_enqueued_at', 1, 0): (
        '32', 'safety',
        'admission queue / cap untrusted timestamp text before ISO-8601 parsing'),
    ('admission.py', '_read_state', 1, 0): (
        '32', 'format',
        'admission ticket / UUID hex identity is exactly 32 lowercase hexadecimal characters'),
    ('admission.py', '_read_state', 1, 1): (
        '2', 'mechanism',
        'admission ticket / at most one role exclusion plus the optional workspace exclusion'),
    ('admission.py', '_Queue.__init__', 1, 0): (
        '2', 'mechanism',
        'admission request / at most one role exclusion plus the optional workspace exclusion'),
    ('project.py', 'Project.backpressure._checked_limit', 2, 0): (
        '1073741824', 'configured policy',
        'backpressure / revalidate the shared configured maximum for open_work_items and producer_inputs_per_source'),
    ('ci_requests.py', 'CiRequests.daily', 2, 0): (
        '100', 'mechanism',
        'CI requests / fixed per-page ceiling bounds the validated dispatch projection'),
    ('ci_requests.py', 'CiRequests.page', 2, 0): (
        '8', 'mechanism',
        'CI cost view / public partial subtotal exposes at most eight dispatches per page'),
    ('ci_requests.py', '_summary', 1, 0): (
        '64', 'safety',
        'CI request record / cap timestamp text before ISO-8601 parsing'),
    ('ci_requests.py', '_summary', 1, 1): (
        '16', 'format',
        'CI request record / covered work items follow the public 16-item request schema'),
    ('ci_requests.py', '_unresolved_summary', 1, 0): (
        '64', 'safety',
        'CI dispatch record / cap timestamp text before ISO-8601 parsing'),
    ('ci_requests.py', '_unresolved_summary', 1, 1): (
        '16', 'format',
        'CI dispatch record / covered work items follow the public 16-item request schema'),
    ('ci_waits.py', '_check_names', 2, 0): (
        '64', 'format',
        'CI wait / number of exact check names follows the public maxItems contract'),
    ('ci_waits.py', '_check_names', 1, 0): (
        '256', 'format',
        'CI wait / individual check names follow the public maxLength contract'),
    ('ci_waits.py', 'readiness_init', 2, 0): (
        '100', 'mechanism',
        'CI wait readiness / fixed maximum page size for the bounded shared delivery scan'),
    ('ci_waits.py', '_shared_page', 2, 0): (
        '100', 'mechanism',
        'CI wait delivery / fixed maximum page size for the bounded shared receipt projection'),
    ('insights.py', '_validate_decision_projection', 1, 0): (
        '64', 'safety', 'decision projection / bound timestamp text before ISO-8601 parsing'),
    ('insights.py', '_validate_pending_projection', 1, 0): (
        '64', 'safety', 'pending insight projection / bound timestamp text before ISO-8601 parsing'),
    ('insights.py', '_check_origin', 2, 0): (
        '256', 'safety', 'insight origin / bound opaque caller label before persistence'),
    ('insights.py', 'Insights.submit', 2, 0): (
        '200', 'format', 'insight submit / title follows the public 1-200 character contract'),
    ('insights.py', 'Insights.revise', 2, 0): (
        '200', 'format', 'insight revision / title follows the public 1-200 character contract'),
    ('insights.py', 'Insights.list_page', 2, 0): (
        '1000', 'format',
        'insight page API / 1-1000 cursor-page contract shared by runtime and CLI'),
    ('insights.py', 'Insights._page_cursor', 2, 0): (
        '4096', 'safety', 'insight cursor / bound encoded cursor text before decoding'),
    ('insights.py', 'Insights._page_cursor', 2, 1): (
        '253402300799', 'format', 'insight cursor / Unix timestamp ceiling for datetime year 9999'),
    ('insights.py', 'Insights.read', 1, 0): (
        '32', 'format', 'insight lookup / only shorter hexadecimal IDs enter prefix resolution'),
    ('insights.py', 'Insights.due_waits', 1, 0): (
        '100', 'mechanism', 'insight waits / fixed maximum per-source delivery page'),
    ('insights.py', 'Insights.pending_obligations', 1, 0): (
        '100', 'mechanism', 'insight obligations / fixed maximum per-source delivery page'),
    ('insights.py', 'Insights.pending_insight_events', 2, 0): (
        '100', 'mechanism', 'pending insight events / fixed maximum delivery page'),
    ('insights.py', 'Insights.shared_decision_page', 2, 0): (
        '100', 'mechanism', 'shared decision events / fixed maximum delivery page'),
    ('insights.py', 'Insights._scan_decision_delivery', 2, 0): (
        '100', 'mechanism', 'decision delivery scan / fixed maximum candidate page'),
    ('insights.py', 'Insights.acknowledge_decisions', 1, 0): (
        '512', 'format',
        'decision acknowledgement / match the public finish ack_events maximum batch size'),
    ('insights.py', 'Insights._archivable', 2, 0): (
        '200', 'format', 'insight archive / title follows the public 1-200 character contract'),
    ('engine_config.py', 'child_authority_digest', 1, 0): (
        '64', 'format',
        'child authority / goal and adapter identity fields are lowercase SHA-256 hex digests'),
    ('engine_config.py', 'child_authority_digest', 1, 1): (
        '1048576', 'safety',
        'child authority / reject an oversized canonical projection before computing its digest'),
    ('engine_config.py', 'session_record', 2, 0): (
        '4096', 'safety',
        'session record / reject oversized persisted session identifiers before resuming'),
    ('engine_config.py', 'session_record', 1, 0): (
        '262144', 'safety',
        'session record / cap canonical persisted data accepted for resume'),
    ('engine_config.py', 'session_generation', 2, 0): (
        '2 ** 31', 'format',
        'session generation / persisted generation.json accepts only a bounded integer counter'),
    ('engine_config.py', 'save_session', 1, 0): (
        '4096', 'safety',
        'session persistence / cap provider-returned identifier text before writing it'),
    ('engine_config.py', 'connected_servers', 1, 0): (
        '64', 'safety',
        'MCP server materialization / retain the explicit-server safety cap at runtime'),
    ('proposal_waits.py', 'normalize', 1, 0): (
        '3', 'format', 'proposal endpoint / repo, ref and SHA must have exactly three components'),
    ('proposal_waits.py', 'delivery', 2, 0): (
        '100', 'mechanism', 'proposal wait / fixed maximum per-source delivery page'),
    ('proposal_waits.py', 'readiness_init', 2, 0): (
        '100', 'mechanism', 'proposal wait readiness / fixed maximum candidate page'),
    ('probes.py', 'sponsor_binding', 1, 0): (
        '65536', 'safety',
        'recovery probes / bound project settings file before parsing and hashing sponsor identity'),
    ('probes.py', '_summary', 2, 0): (
        '2 ** 53', 'format',
        'recovery probes / inclusive exact binary64 integer-magnitude edge for JSON usage values'),
    ('probes.py', '_service', 1, 0): (
        '1024', 'safety',
        'recovery probes / bound admission evidence before reading the saved request count'),
    ('work_items.py', '_open_cursor_key', 1, 0): (
        '4', 'format',
        'work items / cursor token has version, status, timestamp and item-identity fields'),
    ('work_items.py', 'WorkItems.__init__', 2, 0): (
        '31536000', 'configured policy',
        'work items / documented one-year ceiling for the operator-selected, visibility-only stale threshold'),
    ('work_items.py', 'WorkItems._create_id', 2, 0): (
        '128', 'safety',
        'work items / cap caller-supplied idempotency key before canonical hashing'),
    ('work_items.py', 'WorkItems._sidecar_load', 0, 0): (
        '3', 'mechanism',
        'work items / delivery-binding revision cannot predate the schema that introduced it'),
    ('work_items.py', 'WorkItems._legacy_seed', 2, 0): (
        '200', 'format', 'work items / legacy title matches the public title field limit'),
    ('work_items.py', 'WorkItems._legacy_resolution', 1, 0): (
        '4000', 'format', 'work items / stored reconciliation reason matches the public reason field limit'),
    ('work_items.py', 'WorkItems.reconcile', 1, 0): (
        '32', 'format', 'work items / decision event identity is exactly 32 hexadecimal characters'),
    ('work_items.py', 'WorkItems.reconcile', 1, 1): (
        '4000', 'format', 'work items / reconciliation request reason matches the public reason field limit'),
    ('work_items.py', 'WorkItems._submitter_item_status', 1, 0): (
        '4096', 'safety', 'work items / cap canonical submitter-status projection bytes'),
    ('work_items.py', 'WorkItems._page_open_impl', 1, 0): (
        '4', 'mechanism', 'work items / cached projection tuple shape includes its proof witness'),
    ('pi_reselection.py', 'Reselection.bind_session', 1, 0): (
        '4096', 'safety',
        'Pi reselection / bound session-file identity text before retaining it in family state'),
    ('selection.py', 'scalar', 1, 0): (
        '4096', 'safety', 'selection scalar / bound an individual string before canonical encoding'),
    ('selection.py', 'scalar', 1, 1): (
        '2 ** 53', 'format', 'selection scalar / numeric magnitude follows the exact IEEE-754 JSON contract'),
    ('selection.py', 'timestamp', 2, 0): (
        '253402300799', 'format', 'selection evidence / Unix timestamp ceiling for datetime year 9999'),
    ('selection.py', 'validate_condition', 1, 0): (
        '12', 'safety', 'selection conditions / limit recursive policy nesting'),
    ('selection.py', 'validate_condition', 1, 1): (
        '512', 'safety', 'selection condition / bound dotted-path text before splitting'),
    ('selection.py', 'validate_selectors', 1, 0): (
        '65536', 'safety', 'classifier policy / cap the operator-selected policy file before reading'),
    ('selection.py', 'validate_selectors', 0, 0): (
        '256', 'safety', 'classifier inputs / require a minimally useful text budget'),
    ('selection.py', 'validate_selectors', 2, 0): (
        '65536', 'safety', 'classifier inputs / cap per-classifier text collection at 64 KiB'),
    ('selection.py', 'State.read', 1, 0): (
        '128', 'format', 'selection state / run identity follows the bounded run-id format'),
    ('selection.py', 'State.result', 2, 0): (
        '128', 'format', 'selection state / run identity follows the bounded run-id format'),
    ('selection.py', 'State.result', 2, 1): (
        '64', 'mechanism', 'selection state / attempts match the configured on_error retry maximum'),
    ('selection.py', 'validate_event', 2, 0): (
        '4', 'safety', 'probe evidence / cap retained model-request count for one probe event'),
    ('selection.py', 'validate_event', 2, 1): (
        '2 ** 53', 'format', 'probe evidence / token counts follow the exact IEEE-754 JSON contract'),
    ('ci_wake.py', '_ci_rewake_seconds', 0, 0): (
        '60', 'configured policy',
        'CI wake / [vcs] ci_rewake_seconds validated lower interval bound'),
    ('ci_wake.py', '_ci_rewake_seconds', 2, 0): (
        '86400', 'configured policy',
        'CI wake / [vcs] ci_rewake_seconds validated upper interval bound'),
    ('acceptance.py', 'valid_cache_diagnostics', 1, 0): (
        '256', 'safety',
        'acceptance / only expose untrusted relative cache paths within a bounded diagnostic field'),
    ('acceptance.py', 'read_job', 1, 0): (
        '32', 'format',
        'acceptance / retained job command lists follow the public 32-command protocol bound'),
    ('acceptance.py', 'read_job', 1, 1): (
        '32', 'format',
        'acceptance / retained scratch-path manifests follow the 32-entry protocol bound'),
    ('acceptance.py', 'read_job', 1, 2): (
        '255', 'format',
        'acceptance / scratch path component length follows the public 255-character schema bound'),
    ('acceptance.py', '_scope', 2, 0): (
        '32', 'format',
        'acceptance / local command scope follows the public 32-command protocol bound'),
    ('sandbox.py', 'scratch_selection', 1, 0): (
        '32', 'format',
        'sandbox / selected scratch directories follow the public 32-entry protocol bound'),
    ('sandbox.py', 'scratch_selection', 1, 1): (
        '255', 'format',
        'sandbox / scratch directory names follow the public 255-character schema bound'),
    ('check_jobs.py', 'cache_input.unbound', 1, 0): (
        '256', 'safety',
        'check jobs / only expose untrusted relative cache paths within a bounded diagnostic field'),
    ('check_jobs.py', 'submit', 2, 0): (
        '32', 'format',
        'check jobs / custom command arrays follow the public 32-command protocol bound'),
    ('check_jobs.py', 'submit', 1, 0): (
        '32', 'format',
        'check jobs / stored acceptance command lists follow the same 32-command protocol bound'),
    ('check_jobs.py', 'prompt_projection', 1, 0): (
        '65536', 'safety',
        'check jobs / bounded prompt projection metadata ceiling before adding rows'),
    ('check_jobs.py', 'prompt_projection', 1, 1): (
        '65536', 'safety',
        'check jobs / bounded serialized check-job prompt projection ceiling while adding rows'),
    ('check_jobs.py', 'logs', 2, 0): (
        '8192', 'format',
        'check jobs / public command-evidence page-size ceiling'),
    ('ci_acceptance.py', '_text', 2, 0): (
        '1024', 'format',
        'CI acceptance / public text fields allow at most 1024 UTF-8 bytes'),
    ('ci_acceptance.py', '_text', 1, 0): (
        '32', 'format',
        'CI acceptance / reject ASCII C0 control characters in policy text'),
    ('ci_acceptance.py', '_text', 1, 1): (
        '127', 'format',
        'CI acceptance / reject ASCII DEL in policy text'),
    ('ci_acceptance.py', 'policy', 2, 0): (
        '32', 'format',
        'CI acceptance / project verify command list follows the public 32-command protocol bound'),
    ('ci_acceptance.py', 'policy', 2, 1): (
        '32', 'format',
        'CI acceptance / declared equivalence checks follow the 32-entry policy schema'),
    ('ci_acceptance.py', 'policy', 2, 2): (
        '32', 'format',
        'CI acceptance / per-check command indices follow the 32-entry policy schema'),
    ('ci_acceptance.py', 'policy', 2, 3): (
        '32', 'format',
        'CI acceptance / per-check coverage obligations follow the 32-entry policy schema'),
    ('ci_acceptance.py', 'policy', 2, 4): (
        '32', 'format',
        'CI acceptance / per-check environment facts follow the 32-entry policy schema'),
    ('ci_acceptance.py', 'policy', 1, 0): (
        '32768', 'safety',
        'CI acceptance / complete operator policy is capped at the documented 32 KiB'),
    ('ci_acceptance.py', 'validate', 1, 0): (
        '2147483647', 'format',
        'CI acceptance / receipt attempt range is the documented 1-2147483647 integer contract'),
    ('ci_acceptance.py', 'retain', 2, 0): (
        '2147483647', 'format',
        'CI acceptance / ingestion selector uses the same documented attempt range'),
    ('usage.py', '_affinity_row', 2, 0): (
        '256', 'safety',
        'usage / bound the stored role label in the whitelisted affinity projection'),
    ('usage.py', '_affinity_row', 2, 1): (
        '256', 'safety',
        'usage / bound the stored engine label in the whitelisted affinity projection'),
    ('usage.py', '_affinity_row', 2, 2): (
        '64', 'safety',
        'usage / bound recorded start-time text before including it in the affinity projection'),
    ('usage.py', '_affinity_row', 2, 3): (
        '64', 'safety',
        'usage / bound recorded end-time text before including it in the affinity projection'),
    ('usage.py', '_affinity_row', 2, 4): (
        '256', 'safety',
        'usage / bound the stored prompt-mode label in the whitelisted affinity projection'),
    ('usage.py', '_affinity_row', 2, 5): (
        '256', 'safety',
        'usage / bound the stored profile label in the whitelisted affinity projection'),
    ('usage.py', '_affinity_row', 2, 6): (
        '256', 'safety',
        'usage / bound the provider label copied from run evidence'),
    ('usage.py', '_affinity_row', 2, 7): (
        '256', 'safety',
        'usage / bound the model label copied from run evidence'),
    ('codex.py', 'TokenUsage.validate', 2, 0): (
        '2 ** 63 - 1', 'format', 'Codex provider / token counters follow the signed 64-bit integer range'),
    ('usage.py', 'normalize', 2, 0): (
        '2 ** 63 - 1', 'format', 'usage normalization / provider token counters follow the signed 64-bit integer range'),
    ('usage.py', 'attempt_usage', 2, 0): (
        '2 ** 63 - 1', 'format', 'usage aggregation / individual token counts follow the signed 64-bit integer range'),
    ('usage.py', 'attempt_usage', 1, 0): (
        '2 ** 63 - 1', 'format', 'usage aggregation / token totals cannot exceed the signed 64-bit integer range'),
    ('usage.py', 'prompt_input_tokens', 2, 0): (
        '2 ** 63 - 1', 'format', 'prompt usage / input token counts follow the signed 64-bit integer range'),
    ('usage.py', 'prompt_input_tokens', 2, 1): (
        '2 ** 63 - 1', 'format', 'prompt usage / cache-read token counts follow the signed 64-bit integer range'),
    ('usage.py', 'prompt_input_tokens', 2, 2): (
        '2 ** 63 - 1', 'format', 'prompt usage / cache-write token counts follow the signed 64-bit integer range'),
    ('usage.py', '_affinity_row', 2, 8): (
        '2 ** 63 - 1', 'format', 'affinity projection / input tokens follow the signed 64-bit integer range'),
    ('usage.py', '_affinity_row', 2, 9): (
        '2 ** 63 - 1', 'format', 'affinity projection / cache-read tokens follow the signed 64-bit integer range'),
    ('usage.py', '_affinity_row', 2, 10): (
        '2 ** 63 - 1', 'format', 'affinity projection / cache-write tokens follow the signed 64-bit integer range'),
    ('usage.py', 'record_time', 1, 0): (
        '64', 'safety',
        'usage / cap untrusted timestamp text before ISO-8601 parsing'),
    ('usage.py', 'summarize', 1, 0): (
        '1048576', 'safety',
        'usage / fixed per-record read ceiling for retained run evidence; no whole-report byte cap'),
    ('services.py', 'render', 0, 0): (
        '60', 'mechanism',
        'Windows Task Scheduler / repetition intervals have a one-minute minimum'),
    ('services.py', 'render', 2, 0): (
        '31 * 86400', 'mechanism',
        'Windows Task Scheduler / repetition intervals have a 31-day maximum'),
    # VCS adapter text is external-untrusted. These are Mizu's local parser
    # contracts; none asserts an upstream provider maximum.
    ('lineage.py', '_valid_target_ref', 1, 0): (
        '32', 'format',
        'lineage target refs / exclude space and C0 control characters through U+0020'),
    ('lineage.py', '_valid_target_ref', 0, 0): (
        '127', 'format', 'lineage target refs / ASCII DEL starts the rejected DEL/C1 control range'),
    ('lineage.py', '_valid_target_ref', 2, 0): (
        '159', 'format', 'lineage target refs / U+009F ends the rejected DEL/C1 control range'),
    ('lineage.py', '_check_common', 1, 0): (
        '40', 'safety',
        'lineage observations / cap untrusted timestamp text before ISO-8601 parsing'),
    ('vcs.py', '_reject_terminal_controls', 1, 0): (
        '32', 'format',
        'VCS forge text / first ASCII C0 control code boundary'),
    ('vcs.py', '_reject_terminal_controls', 1, 1): (
        '127', 'format',
        'VCS forge text / ASCII DEL control code'),
    ('vcs.py', '_reject_terminal_controls', 0, 0): (
        '128', 'format',
        'VCS forge text / first ASCII C1 control code boundary'),
    ('vcs.py', '_reject_terminal_controls', 2, 0): (
        '159', 'format',
        'VCS forge text / last ASCII C1 control code boundary'),
    ('vcs.py', '_check_ref_name', 1, 0): (
        '512', 'safety',
        'VCS refs / local per-name bound before path and response processing'),
    ('vcs.py', 'check_branch', 1, 0): (
        '256', 'format',
        'VCS branch / shared public branch-name character bound'),
    ('vcs.py', 'require_go_approval', 1, 0): (
        '128', 'safety',
        'VCS approval lookup / bound an untrusted proposal identity before matching'),
    ('vcs.py', 'parse_acquired', 1, 0): (
        '128', 'safety',
        'VCS acquired content / bound the returned untrusted proposal identity'),
    ('vcs.py', 'parse_acquired', 1, 1): (
        '64', 'format',
        'VCS acquired content / SHA-256 digest has exactly 64 lowercase hex characters'),
    ('vcs.py', 'check_ci_name', 1, 0): (
        '256', 'format',
        'VCS CI check name / shared public check-name character bound'),
    ('vcs.py', 'read_via', 1, 0): (
        '128', 'safety',
        'VCS read / bound an untrusted proposal identifier before adapter use'),
    ('vcs.py', '_ci_timestamp', 1, 0): (
        '64', 'safety',
        'VCS CI status / cap untrusted timestamp text before ISO-8601 parsing'),
    ('vcs.py', 'parse_status_checks', 1, 0): (
        '64', 'safety',
        'VCS CI status / bound untrusted status text before retention'),
    ('vcs.py', 'parse_status_checks', 1, 1): (
        '4096', 'safety',
        'VCS CI status / bound untrusted URL text before reporting'),
    ('vcs.py', 'parse_status_checks', 1, 2): (
        '128', 'safety',
        'VCS CI status / bound untrusted run identity before binding'),
    ('vcs.py', 'parse_status_checks', 2, 0): (
        '1073741824', 'mechanism',
        'VCS CI status / local positive run-attempt field ceiling before identity binding'),
    ('vcs.py', 'parse_status_checks', 1, 3): (
        '256', 'safety',
        'VCS CI status / bound untrusted repository namespace text'),
    ('vcs.py', '_check_repo', 1, 0): (
        '256', 'safety',
        'VCS proposal endpoints / bound repository text from the external observation'),
    ('vcs.py', '_check_review', 1, 0): (
        '128', 'safety',
        'VCS proposal review / bound the untrusted reviewer identity'),
    ('vcs.py', 'parse_proposals', 1, 0): (
        '128', 'safety',
        'VCS proposals / bound each untrusted proposal identity before storage'),
    ('vcs.py', 'parse_proposals', 1, 1): (
        '4096', 'safety',
        'VCS proposals / bound untrusted URL text before storage'),
    ('vcs.py', 'proposal_insight_id', 1, 0): (
        '128', 'safety',
        'VCS proposal identity / bound external identifier before deterministic mapping'),
    ('vcs.py', '_check_prefix', 1, 0): (
        '256', 'safety',
        'VCS ref selection / bound local prefix text before matching refs'),
    ('vcs.py', 'retire_via', 1, 0): (
        '128', 'safety',
        'VCS retirement / bound the external ref identity before matching'),
    ('vcs.py', 'close_if_adopted_via', 1, 0): (
        '128', 'safety',
        'VCS conditional close / bound the external proposal identity before matching'),
    ('vcs.py', 'dispose_via', 1, 0): (
        '128', 'safety',
        'VCS disposal / bound the external proposal identity before matching'),
    ('vcs.py', 'validate_ci_request', 1, 0): (
        '128', 'format',
        'VCS CI request / workflow name follows the public request schema'),
    ('vcs.py', 'validate_ci_runs', 1, 0): (
        '128', 'format',
        'VCS CI response / workflow name matches the public request format'),
    ('vcs.py', 'validate_ci_runs', 1, 1): (
        '128', 'safety',
        'VCS CI response / bound untrusted run identity before exact-SHA binding'),
    ('vcs.py', 'validate_ci_runs', 1, 2): (
        '4096', 'safety',
        'VCS CI response / bound untrusted URL text before recording'),
}


def numeric(node, symbolic=()):
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float)
    if isinstance(node, ast.UnaryOp):
        return numeric(node.operand, symbolic)
    if isinstance(node, ast.BinOp):
        return numeric(node.left, symbolic) and numeric(node.right, symbolic)
    if isinstance(node, ast.Call):
        if node.keywords:
            return False
        if isinstance(node.func, ast.Name) and node.func.id == 'min':
            return len(node.args) >= 2 and all(numeric(arg, symbolic) for arg in node.args)
        if (isinstance(node.func, ast.Name) and node.func.id == 'len'
                and len(node.args) == 1):
            argument = node.args[0]
            if isinstance(argument, ast.Name) and argument.id in symbolic:
                return True
            if (isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name)
                    and argument.func.id == 'canonical' and len(argument.args) == 1
                    and not argument.keywords and isinstance(argument.args[0], ast.Dict)):
                envelope = argument.args[0]
                if not all(isinstance(key, ast.Constant) and isinstance(key.value, str)
                           for key in envelope.keys):
                    return False
                for value in envelope.values:
                    if isinstance(value, ast.List) and not value.elts:
                        continue
                    if isinstance(value, ast.Constant) and type(value.value) in (str, int):
                        continue
                    if isinstance(value, ast.Name) and value.id in symbolic:
                        continue
                    return False
                return True
        return False
    if isinstance(node, ast.Attribute):
        return ast.unparse(node) in symbolic
    return isinstance(node, ast.Name) and node.id in symbolic


def comparison_argument_inventory(tree, *, minimum_magnitude=3):
    """Inventory direct numeric comparison operands at or above a scoped threshold."""
    found, occurrences = {}, {}

    def literal_value(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            operand = literal_value(node.operand)
            if operand is None:
                return None
            value = -operand if isinstance(node.op, ast.USub) else operand
        elif isinstance(node, ast.BinOp):
            left, right = literal_value(node.left), literal_value(node.right)
            if left is None or right is None:
                return None
            try:
                if isinstance(node.op, ast.Add):
                    value = left + right
                elif isinstance(node.op, ast.Sub):
                    value = left - right
                elif isinstance(node.op, ast.Mult):
                    value = left * right
                elif isinstance(node.op, ast.FloorDiv):
                    value = left // right
                elif isinstance(node.op, ast.Div):
                    value = left / right
                elif isinstance(node.op, ast.Mod):
                    value = left % right
                elif isinstance(node.op, ast.Pow):
                    # Fold only small integer powers; unknown expressions stay out.
                    if (type(left) is not int or type(right) is not int
                            or not 0 <= right <= 63 or abs(left) > 2**63):
                        return None
                    value = left ** right
                    if abs(value) > 2**63:
                        return None
                else:
                    return None
            except (ArithmeticError, OverflowError):
                return None
        else:
            return None
        if type(value) is int and abs(value) > 2**63:
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    class Comparisons(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Compare(self, node):
            scope = '.'.join(self.scope) or '<module>'
            for index, operand in enumerate([node.left, *node.comparators]):
                value = literal_value(operand)
                if value is None or abs(value) < minimum_magnitude:
                    continue
                base = (scope, index)
                occurrence = occurrences.get(base, 0)
                occurrences[base] = occurrence + 1
                found[(*base, occurrence)] = ast.unparse(operand)
            self.generic_visit(node)

    Comparisons().visit(tree)
    return found


def range_argument_inventory(tree, *, symbolic=False):
    """Inventory numeric or non-numeric range arguments; symbolic selects the latter."""
    found, occurrences = {}, {}

    class RangeArguments(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == 'range':
                scope = '.'.join(self.scope) or '<module>'
                for index, argument in enumerate(node.args):
                    is_numeric = numeric(argument)
                    if symbolic:
                        if is_numeric:
                            continue
                    elif not is_numeric:
                        continue
                    base = (scope, index)
                    occurrence = occurrences.get(base, 0)
                    occurrences[base] = occurrence + 1
                    site = (*base, occurrence)
                    found[site] = ast.unparse(argument)
            self.generic_visit(node)

    RangeArguments().visit(tree)
    return found


SYMBOLIC_BOUNDS = {
    'acceptance.py': {
        'MAX_ACCEPTANCE_SCOPE_BYTES': frozenset({'MAX_BYTES'}),
    },
    'editor.py': {
        'EDITOR_SEARCH_RESPONSE_BYTES': frozenset({'MAX_FRAME'}),
        'EDITOR_SEARCH_SKIPPED_BYTES': frozenset({'EDITOR_SEARCH_RESPONSE_BYTES'}),
    },
    'fs.py': {'_LOCAL_BLOCKING_TIMEOUT': frozenset({'_platform.LOCK_TIMEOUT'})},
    'config.py': {
        'MAX_EDITOR_SEARCH_MATCH_CHARS': frozenset({'READ_PAGE_CHARS'}),
        'MAX_NO_PROGRESS_BACKOFF_SECONDS': frozenset({'MAX_WAIT_SECONDS'}),
        'MAX_USAGE_POLICY_COUNT': frozenset({'TOML_INTEGER_MAX', 'sys.maxsize'}),
        'MAX_USAGE_RUNS_SCANNED': frozenset({'MAX_USAGE_POLICY_COUNT'}),
        'MAX_DISPATCH_EVIDENCE_COUNT': frozenset({'TOML_INTEGER_MAX', 'sys.maxsize'}),
        'MAX_DISPATCH_EVIDENCE_BYTES': frozenset({'TOML_INTEGER_MAX', 'sys.maxsize'}),
        'MIN_DISPATCH_EVIDENCE_BYTES': frozenset({'TOML_INTEGER_MAX'}),
    },
    'lineage.py': {
        'TARGET_REF_MAX': frozenset({'TARGET_REF_PREFIX', 'TARGET_BRANCH_MAX'}),
    },
    'storage_runs.py': {
        'MAX_ENTRIES': frozenset({'MAX_STORAGE_RUN_ENTRIES'}),
    },
    'check_jobs.py': {
        'CHECK_INPUT_HASH_CHUNK_BYTES': frozenset({'MEBIBYTE_BYTES'}),
        'COMMAND_EVIDENCE_ENVELOPE_BYTES': frozenset({'MEBIBYTE_BYTES'}),
    },
    'storage.py': {
        '_SNAPSHOT_AUDIT_BYTES': frozenset({
            '_AUDIT_JSON_BYTES', 'MAX_PROJECT_GOAL_BYTES', 'MAX_SNAPSHOT_GOAL_JSON_EXPANSION'}),
    },
    'ci_observation.py': {
        'MAX_CI_WAKE_BRANCHES': frozenset({'MAX_CI_WAKE_EXTRA_BRANCHES'}),
        'MAX_CI_CHECKS_PER_BRANCH': frozenset({'DEFAULT_CI_CACHE_MAX_CHECKS_PER_BRANCH'}),
        'MAX_CI_FAILURE_BRANCHES': frozenset({'DEFAULT_CI_CACHE_MAX_BRANCHES'}),
        'MAX_CI_WAKE_ROWS': frozenset({'MAX_CI_CACHE_ROWS'}),
    },
    'web_worker.py': {
        'MAX_WEB_WORKER_INPUT_BYTES': frozenset({'MAX_INCLUDE_BYTES'}),
    },
    'work_items.py': {
        'MAX_DELIVERED_WAIT_RESOLUTION_TOKENS': frozenset({'MAX_OPEN_PAGE_ROWS'}),
        'MAX_EVENT_BYTES': frozenset({'PREVIEW_BYTES', 'MAX_RESOLUTION_BYTES'}),
        'MAX_PROJECTION_BYTES': frozenset({'MAX_EVENT_BYTES'}),
        'MAX_HISTORY_PAGE_BYTES': frozenset({'MAX_OPEN_PAGE_BYTES'}),
        'MAX_CLOSED_ROW_BYTES': frozenset({'MAX_OPEN_ROW_BYTES'}),
        'MAX_CLOSED_PAGE_BYTES': frozenset({'MAX_OPEN_PAGE_BYTES'}),
        '_SIDECAR_MAX_BYTES': frozenset({'PREVIEW_BYTES'}),
    },
    'delegation_jobs.py': {
        'MAX_STATE_BYTES': frozenset({'MAX_BINDING_BYTES', '_INTENT_NULL_OVERHEAD', 'MAX_JOBS'}),
    },
}


def inventory(tree, extras=(), symbolic_bounds=None):
    found = {}
    symbolic_bounds = symbolic_bounds or {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            symbolic = symbolic_bounds.get(target.id, ())
            if ((target.id in extras or CANDIDATE.fullmatch(target.id))
                    and (numeric(value) or (symbolic and numeric(value, symbolic)))):
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
            scope = '.'.join(self.scope) or '<module>'
            maximum = node.args[1] if callee == 'bounded' and len(node.args) > 1 else next(
                (keyword.value for keyword in node.keywords
                 if callee == 'bounded' and keyword.arg == 'maximum'), None)
            if maximum is not None:
                base = (scope, callee, 'maximum')
                index = occurrences.get(base, 0)
                occurrences[base] = index + 1
                found[(*base, index)] = ast.unparse(maximum)
            for keyword in node.keywords:
                has_literal = any(
                    isinstance(value, ast.Constant) and type(value.value) in (int, float)
                    for value in ast.walk(keyword.value))
                if (keyword.arg not in CALL_KEYWORDS or not has_literal
                        or callee == 'bounded' and keyword.arg == 'maximum'):
                    continue
                base = (scope, callee, keyword.arg)
                index = occurrences.get(base, 0)
                occurrences[base] = index + 1
                found[(*base, index)] = ast.unparse(keyword.value)
            self.generic_visit(node)

    Calls().visit(tree)
    return found


def timed_wait_inventory(tree):
    """Inventory selected timed-wait positional arguments without evaluation."""
    found, occurrences = {}, {}

    class TimedWaits(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'wait', 'sleep'}:
                callee = callee_name(node.func)
                for argument_index, argument in enumerate(node.args):
                    if not any(isinstance(value, ast.Constant)
                               and type(value.value) in (int, float)
                               for value in ast.walk(argument)):
                        continue
                    base = ('.'.join(self.scope) or '<module>', callee, argument_index)
                    occurrence = occurrences.get(base, 0)
                    occurrences[base] = occurrence + 1
                    found[(*base, occurrence)] = ast.unparse(argument)
            self.generic_visit(node)

    TimedWaits().visit(tree)
    return found


def parameter_default_inventory(tree):
    found = {}

    class Parameters(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            positional = node.args.posonlyargs + node.args.args
            # Defaults belong to the last N positional parameters, across '/'.
            start = len(positional) - len(node.args.defaults)
            pairs = list(zip(positional[start:], node.args.defaults))
            pairs.extend(zip(node.args.kwonlyargs, node.args.kw_defaults))
            for parameter, default in pairs:
                if not PARAMETER_BOUND.search(parameter.arg) or not numeric(default):
                    continue
                site = ('.'.join(self.scope), parameter.arg)
                if site in found:
                    raise AssertionError(f'Duplicate parameter default site: {site}')
                found[site] = ast.unparse(default)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    Parameters().visit(tree)
    return found


class RuntimeBoundDeclarationTests(unittest.TestCase):
    def test_timed_wait_argument_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for expression, category, rationale in TIMED_WAIT_ARGUMENTS.values():
            self.assertIn(category, categories)
            self.assertTrue(rationale.strip())
        found = {}
        for path in sorted(ROOT.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for site, expression in timed_wait_inventory(tree).items():
                found[(path.name, *site)] = expression
        self.assertEqual(found, {site: entry[0] for site, entry in TIMED_WAIT_ARGUMENTS.items()})

    def test_timed_wait_detector_scope_and_mismatch_detection(self):
        source = """
def outer():
    stop.wait(0.01)
    stop.wait(timeout=0.2)
    process.wait(timeout=0.5)
    time.sleep(min(0.05, remaining))
    text = '.'.join(parts)

def sibling():
    waiter.wait(0.02)
"""
        expected = {
            ('outer', 'stop.wait', 0, 0): '0.01',
            ('outer', 'time.sleep', 0, 0): 'min(0.05, remaining)',
            ('sibling', 'waiter.wait', 0, 0): '0.02',
        }
        self.assertEqual(timed_wait_inventory(ast.parse(source)), expected)
        for changed in (source + "\ndef extra():\n    stop.wait(0.03)\n",
                        source.replace('time.sleep(min(0.05, remaining))',
                                       'time.sleep(min(0.1, remaining))'),
                        source.replace('stop.wait(0.01)', 'stop.wait(0.005)')):
            with self.subTest(changed=changed):
                self.assertNotEqual(timed_wait_inventory(ast.parse(changed)), expected)

    def test_parameter_default_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for expression, category, rationale in PARAMETER_DEFAULTS.values():
            self.assertIn(category, categories)
            self.assertTrue(rationale.strip())
        found = {}
        for path in sorted(ROOT.rglob('*.py')):
            filename = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for (scope, parameter), expression in parameter_default_inventory(tree).items():
                found[(filename, scope, parameter)] = expression
        self.assertEqual(found, {site: entry[0] for site, entry in PARAMETER_DEFAULTS.items()})

    def test_range_argument_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for expression, category, rationale in RANGE_ARGUMENTS.values():
            self.assertIn(category, categories)
            self.assertTrue(rationale.strip())
        found = {}
        for path in sorted(ROOT.rglob('*.py')):
            filename = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for (scope, argument, occurrence), expression in range_argument_inventory(tree).items():
                found[(filename, scope, argument, occurrence)] = expression
        self.assertEqual(found, {site: entry[0] for site, entry in RANGE_ARGUMENTS.items()})

    def test_work_item_read_view_cache_bounds_are_declared_and_used(self):
        from mizu.config import TOML_INTEGER_MAX, _LIMIT_RANGES

        config_tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in config_tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        defaults = {node.target.id: ast.unparse(node.value) for node in limits.body
                    if isinstance(node, ast.AnnAssign)}
        for name, (expression, category, rationale) in WORK_ITEM_READ_VIEW_CACHE.items():
            self.assertEqual(category, 'configured policy')
            self.assertTrue(rationale.strip())
            self.assertEqual(defaults[name], expression)
            self.assertEqual(_LIMIT_RANGES[name], (1, TOML_INTEGER_MAX))
        tree = ast.parse((ROOT / 'work_item_read_view.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'readonly_view')
        calls = [node for node in ast.walk(function)
                 if isinstance(node, ast.Call) and callee_name(node.func) == '_Records']
        self.assertEqual([ast.unparse(call.args[1]) for call in calls], [
            'limits.work_item_read_view_snapshot_cache_bytes',
            'limits.work_item_read_view_file_cache_bytes'])
        comparisons = {ast.unparse(node) for node in ast.walk(function)
                       if isinstance(node, ast.Compare)}
        self.assertIn('size > limits.work_item_read_view_item_cache_bytes', comparisons)
        self.assertIn('len(items) >= limits.work_item_read_view_item_cache_entries', comparisons)
        self.assertIn('item_bytes + size > limits.work_item_read_view_item_cache_bytes', comparisons)

    def test_wait_event_limit_is_configured_and_used_by_prompt_projections(self):
        from mizu.config import _LIMIT_RANGES

        config_tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in config_tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        defaults = {node.target.id: ast.unparse(node.value) for node in limits.body
                    if isinstance(node, ast.AnnAssign)}
        self.assertEqual(defaults['wait_events_limit'], '10')
        self.assertEqual(_LIMIT_RANGES['wait_events_limit'], (1, 100))

        runtime = ast.parse((ROOT / 'runtime.py').read_text(encoding='utf-8'))
        dispatch = next(node for node in ast.walk(runtime)
                        if isinstance(node, ast.FunctionDef) and node.name == '_dispatch_state')
        self.assertIn('wait_limit = project.config.limits.wait_events_limit',
                      {ast.unparse(node) for node in ast.walk(dispatch)
                       if isinstance(node, (ast.Assign, ast.AnnAssign))})
        configured_calls = {
            ast.unparse(call.func): {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
            for call in ast.walk(dispatch) if isinstance(call, ast.Call)
            and ast.unparse(call.func) in {
                'ci_waits.delivery', 'proposal_waits.delivery',
                'project.insights.due_waits', 'project.insights.pending_obligations'}
        }
        self.assertEqual(configured_calls, {
            'ci_waits.delivery': {'limit': 'wait_limit'},
            'proposal_waits.delivery': {'limit': 'wait_limit'},
            'project.insights.due_waits': {'limit': 'wait_limit'},
            'project.insights.pending_obligations': {'limit': 'wait_limit'},
        })
        work_order = ast.parse((ROOT / 'work_order.py').read_text(encoding='utf-8'))
        fresh = next(node for node in ast.walk(work_order)
                     if isinstance(node, ast.FunctionDef) and node.name == '_fresh_generation_for_role')
        self.assertIn('wait_limit = project.config.limits.wait_events_limit',
                      {ast.unparse(node) for node in ast.walk(fresh)
                       if isinstance(node, (ast.Assign, ast.AnnAssign))})
        fresh_calls = {
            ast.unparse(call.func): {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
            for call in ast.walk(fresh) if isinstance(call, ast.Call)
            and ast.unparse(call.func) in {
                'project.insights.due_waits',
                'project.insights.pending_obligations',
                '_ciw.pending', '_pww.pending'}
        }
        self.assertEqual(fresh_calls, {
            'project.insights.due_waits': {'limit': 'wait_limit'},
            'project.insights.pending_obligations': {'limit': 'wait_limit'},
            '_ciw.pending': {'limit': 'wait_limit'},
            '_pww.pending': {'limit': 'wait_limit'},
        })

    def test_editor_search_bounds_are_declared_and_used(self):
        tree = ast.parse((ROOT / 'editor.py').read_text(encoding='utf-8'))
        capsule = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == 'Capsule')
        call = next(node for node in capsule.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'call')
        search = next(node for node in ast.walk(call)
                      if isinstance(node, ast.If)
                      and any(isinstance(value, ast.Constant)
                              and value.value == 'search_files'
                              for value in ast.walk(node.test)))
        comparisons = {ast.unparse(node) for node in ast.walk(search)
                       if isinstance(node, ast.Compare)}
        self.assertIn("scanned + entry['bytes'] > self.editor.search_max_bytes", comparisons)
        self.assertIn('len(hits) == self.editor.search_max_matches', comparisons)
        slices = {ast.unparse(node) for node in ast.walk(search)
                  if isinstance(node, ast.Subscript)
                  and ast.unparse(node.value) == 'value'}
        self.assertIn('value[:self.editor.search_match_chars]', slices)
        self.assertIn("entry['bytes'] > PREVIEW_BYTES", comparisons)
        self.assertIn('result_bytes + size > EDITOR_SEARCH_RESPONSE_BYTES', comparisons)
        self.assertIn('skipped_bytes + size > skipped_budget', comparisons)
        self.assertIn('append_bounded(skipped, path, skipped_budget=EDITOR_SEARCH_SKIPPED_BYTES)',
                      {ast.unparse(node) for node in ast.walk(search) if isinstance(node, ast.Call)})
        calls = {ast.unparse(node) for node in ast.walk(search) if isinstance(node, ast.Call)}
        self.assertIn('append_bounded(skipped, path, skipped_budget=EDITOR_SEARCH_SKIPPED_BYTES)', calls)
        self.assertIn('append_bounded(hits, hit)', calls)
        self.assertIn("safe_read(self.bundle, 'editor-search.json', EDITOR_SEARCH_SETTINGS_BYTES)",
                      {ast.unparse(node) for node in ast.walk(capsule) if isinstance(node, ast.Call)})

    def test_bridge_listeners_share_declared_backlog_bound(self):
        tree = ast.parse((ROOT / 'bridge.py').read_text(encoding='utf-8'))
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name not in {
                    '_UnixBridgeServer', '_TCPBridgeServer'}:
                continue
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                if any(isinstance(target, ast.Name) and target.id == 'request_queue_size'
                       for target in statement.targets):
                    found[node.name] = ast.unparse(statement.value)
        self.assertEqual(found, {
            '_UnixBridgeServer': 'BRIDGE_BACKLOG_LIMIT',
            '_TCPBridgeServer': 'BRIDGE_BACKLOG_LIMIT',
        })

    def test_selected_comparison_bound_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for expression, category, rationale in COMPARISON_BOUND_ARGUMENTS.values():
            self.assertIn(category, categories)
            self.assertTrue(rationale.strip())
        found = {}
        for filename in (
                'acceptance.py', 'bridge.py', 'check_jobs.py', 'ci_acceptance.py',
                'ci_observation.py', 'ci_requests.py', 'ci_waits.py', 'ci_wake.py', 'claude.py',
                'continuations.py', 'delegation_jobs.py', 'engine_config.py', 'errors.py',
                'explain.py', 'insights.py', 'proposal_waits.py', 'selection.py',
                'cli.py', 'codex.py', 'usage.py', 'config.py', 'runtime.py',
                'pi_reselection.py', 'probes.py', 'project.py', 'admission.py', 'services.py', 'vcs.py',
                'work_items.py', 'persistent_exec.py', 'sandbox.py', 'lineage.py',
                'operator_records.py'):
            tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
            minimum = (1 if filename == 'bridge.py'
                       else 2 if filename == 'admission.py' else 3)
            for site, expression in comparison_argument_inventory(
                    tree, minimum_magnitude=minimum).items():
                found[(filename, *site)] = expression
        self.assertEqual(found, {
            site: entry[0] for site, entry in COMPARISON_BOUND_ARGUMENTS.items()})

    def test_comparison_detector_operand_scope_and_drift(self):
        source = (
            "def outer(value):\n"
            "    if 1 <= value <= 32:\n"
            "        return value\n"
            "    def nested():\n"
            "        return value > 64\n"
            "    return value <= 31 * 86400\n"
            "class Reader:\n"
            "    def read(self):\n"
            "        return 128 >= 0\n"
        )
        self.assertEqual(comparison_argument_inventory(ast.parse(source)), {
            ('outer', 2, 0): '32',
            ('outer', 1, 0): '31 * 86400',
            ('outer.nested', 1, 0): '64',
            ('Reader.read', 0, 0): '128',
        })
        changed = source.replace('value > 64', 'value > 96')
        self.assertNotEqual(comparison_argument_inventory(ast.parse(changed)),
                            comparison_argument_inventory(ast.parse(source)))
        changed_arithmetic = source.replace('31 * 86400', '30 * 86400')
        self.assertNotEqual(comparison_argument_inventory(ast.parse(changed_arithmetic)),
                            comparison_argument_inventory(ast.parse(source)))
        small_bound = ast.parse('def queue(resources): return len(resources) > 2')
        self.assertEqual(comparison_argument_inventory(small_bound), {})
        self.assertEqual(comparison_argument_inventory(small_bound, minimum_magnitude=2),
                         {('queue', 1, 0): '2'})
        arithmetic_bound = ast.parse('def queue(resources): return len(resources) > 1 + 2')
        self.assertEqual(comparison_argument_inventory(arithmetic_bound),
                         {('queue', 1, 0): '1 + 2'})

    def test_comparison_detector_handles_only_bounded_integer_powers(self):
        small = ast.parse('def scalar(value): return value <= 2 ** 53')
        self.assertEqual(comparison_argument_inventory(small), {
            ('scalar', 1, 0): '2 ** 53',
        })
        large = ast.parse('def scalar(value): return value <= 2 ** 1000000000')
        self.assertEqual(comparison_argument_inventory(large), {})

        over = ast.parse('def scalar(value): return value <= 2 ** 63 + 1')
        self.assertEqual(comparison_argument_inventory(over), {})

    def test_symbolic_range_argument_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for expression, category, rationale in SYMBOLIC_RANGE_ARGUMENTS.values():
            self.assertIn(category, categories)
            self.assertTrue(rationale.strip())
        found = {}
        for path in sorted(ROOT.rglob('*.py')):
            filename = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for (scope, argument, occurrence), expression in range_argument_inventory(
                    tree, symbolic=True).items():
                found[(filename, scope, argument, occurrence)] = expression
        self.assertEqual(found, {site: entry[0] for site, entry in SYMBOLIC_RANGE_ARGUMENTS.items()})

    def test_symbolic_range_detector_scope_and_drift(self):
        source = (
            "def outer(count, rows):\n"
            "    range(0, count, 2)\n"
            "    def nested():\n"
            "        return range(len(rows))\n"
            "class Reader:\n"
            "    def read(self):\n"
            "        return range(self.capacity)\n"
        )
        self.assertEqual(range_argument_inventory(ast.parse(source), symbolic=True), {
            ('outer', 1, 0): 'count',
            ('outer.nested', 0, 0): 'len(rows)',
            ('Reader.read', 0, 0): 'self.capacity',
        })
        changed = source.replace('range(len(rows))', 'range(len(rows) - 1)')
        self.assertNotEqual(range_argument_inventory(ast.parse(changed), symbolic=True),
                            range_argument_inventory(ast.parse(source), symbolic=True))

    def test_range_detector_scope_argument_alignment_and_numeric_vocabulary(self):
        source = '''
def outer(count):
    range(1, count, 2 * 1024)
    def nested():
        return range(-1, -1)
class Reader:
    def read(self):
        return range(0, 4)
'''
        self.assertEqual(range_argument_inventory(ast.parse(source)), {
            ('outer', 0, 0): '1',
            ('outer', 2, 0): '2 * 1024',
            ('outer.nested', 0, 0): '-1',
            ('outer.nested', 1, 0): '-1',
            ('Reader.read', 0, 0): '0',
            ('Reader.read', 1, 0): '4',
        })

    def test_range_detector_rejects_drift(self):
        source = 'def read(count): return range(1, count, 4)'
        expected = range_argument_inventory(ast.parse(source))
        for changed in (
                source.replace('range(1, count, 4)', 'range(1, count, 8)'),
                source.replace('range(1, count, 4)', 'range(1, count)'),
                source.replace('return range', 'return list'),
                source.replace('def read', 'def renamed')):
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                self.assertEqual(range_argument_inventory(ast.parse(changed)), expected)

    def test_parameter_detector_alignment_scope_and_vocabulary(self):
        source = '''
def outer(unrelated, max_bytes=2 * 1024, /, offset=0, *args,
          required, limit=3, timeout=None, depth=-1, count=True,
          size=DEFAULT_SIZE, rows=int(4), discount=5, **kwargs):
    async def nested(*, seconds=0.5):
        pass
    class Inner:
        def method(self, retention_days=31):
            pass

class Reader:
    async def read(self, required, /, *, bytes=8):
        pass

def no_defaults(limit, /, count, *, size):
    pass
'''
        self.assertEqual(parameter_default_inventory(ast.parse(source)), {
            ('outer', 'max_bytes'): '2 * 1024',
            ('outer', 'offset'): '0',
            ('outer', 'limit'): '3',
            ('outer', 'depth'): '-1',
            ('outer.nested', 'seconds'): '0.5',
            ('outer.Inner.method', 'retention_days'): '31',
            ('Reader.read', 'bytes'): '8',
        })
        names = ['limit', 'max', 'maximum', 'cap', 'timeout', 'seconds', 'days',
                 'bytes', 'size', 'depth', 'count', 'entries', 'rows', 'offset',
                 'retries', 'budget', 'retention', 'threshold', 'file_limit',
                 'max_items', 'timeout_ms', 'retention_days', 'byte_limit']
        source = 'def bounds(' + ', '.join(f'{name}=1' for name in names) + '): pass'
        self.assertEqual(parameter_default_inventory(ast.parse(source)), {
            ('bounds', name): '1' for name in names
        })

    def test_parameter_detector_rejects_drift_and_duplicate_sites(self):
        source = 'def read(required, /, limit=2 * 1024, *, offset=0): pass'
        expected = parameter_default_inventory(ast.parse(source))
        for changed in (
                source.replace('offset=0', 'offset=0, timeout=1'),
                source.replace('limit=2 * 1024, ', ''),
                source.replace('2 * 1024', '2048'),
                source.replace('def read', 'def renamed'),
                source.replace('limit=', 'cap=')):
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                self.assertEqual(parameter_default_inventory(ast.parse(changed)), expected)
        with self.assertRaisesRegex(AssertionError, 'Duplicate parameter default site'):
            parameter_default_inventory(ast.parse(
                'class Reader:\n'
                '    def read(self, *, limit=1): pass\n'
                '    async def read(self, *, limit=2): pass\n'))

    def test_declared_inventory(self):
        categories = {'policy', 'configured policy', 'mechanism', 'format', 'safety'}
        for filename, declarations in DECLARATIONS.items():
            with self.subTest(file=filename):
                for expression, category, rationale in declarations.values():
                    self.assertIn(category, categories)
                    self.assertTrue(rationale.strip())
                tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
                self.assertEqual(
                    inventory(tree, EXTRA_CANDIDATES.get(filename, ()),
                              SYMBOLIC_BOUNDS.get(filename)),
                    {name: entry[0] for name, entry in declarations.items()})


    def test_acceptance_scope_uses_named_receipt_envelope(self):
        tree = ast.parse((ROOT / 'acceptance.py').read_text(encoding='utf-8'))
        scope = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == '_scope')
        bounds = [ast.unparse(comparator)
                  for node in ast.walk(scope) if isinstance(node, ast.Compare)
                  for operator, comparator in zip(node.ops, node.comparators)
                  if isinstance(operator, ast.Gt)]
        self.assertEqual(bounds, ['MAX_ACCEPTANCE_SCOPE_BYTES',
                                  'MAX_ACCEPTANCE_SCOPE_BYTES'])

    def test_acceptance_cache_diagnostics_use_named_entry_bound(self):
        tree = ast.parse((ROOT / 'acceptance.py').read_text(encoding='utf-8'))
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == 'valid_cache_diagnostics')
        bounds = [ast.unparse(comparator)
                  for node in ast.walk(function) if isinstance(node, ast.Compare)
                  for operator, comparator in zip(node.ops, node.comparators)
                  if isinstance(operator, ast.LtE)
                  and ast.unparse(node.left) == 'len(diagnostics)']
        self.assertEqual(bounds, ['MAX_CACHE_DIAGNOSTIC_ENTRIES'])

    def test_restore_streaming_uses_declared_chunk_bound(self):
        tree = ast.parse((ROOT / 'storage.py').read_text(encoding='utf-8'))
        restore = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef) and node.name == 'restore')
        reads = [node for node in ast.walk(restore)
                 if isinstance(node, ast.Call) and callee_name(node.func) == 'stream.read']
        self.assertEqual(len(reads), 1)
        self.assertEqual(ast.unparse(reads[0].args[0]),
                         'min(RESTORE_CHUNK_BYTES, remaining)')
        self.assertEqual(DECLARATIONS['storage.py']['RESTORE_CHUNK_BYTES'][0], '1048576')

    def test_check_input_ceiling_is_shared_by_config_and_readers(self):
        sites = (('acceptance.py', 'read_job'),
                 ('check_jobs.py', '_mount_fingerprint'))
        for filename, scope_name in sites:
            tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
            function = next(node for node in ast.walk(tree)
                            if isinstance(node, ast.FunctionDef) and node.name == scope_name)
            uses = [node for node in ast.walk(function)
                    if isinstance(node, ast.Compare)
                    and 'MAX_CHECK_INPUT_BOUND' in
                    [ast.unparse(value) for value in node.comparators]]
            self.assertEqual(len(uses), 1, (filename, scope_name))
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        uses = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and callee_name(node.func) == 'number'
                and node.args and ast.unparse(node.args[-1]) == 'MAX_CHECK_INPUT_BOUND']
        self.assertEqual(len(uses), 1)
        self.assertEqual(CONFIG_MODULE_BOUNDS['MAX_CHECK_INPUT_BOUND'][:2],
                         ('1 << 40', 'safety'))

    def test_check_job_stream_and_evidence_bounds_use_declared_limits(self):
        tree = ast.parse((ROOT / 'check_jobs.py').read_text(encoding='utf-8'))
        fingerprint = next(node for node in ast.walk(tree)
                           if isinstance(node, ast.FunctionDef)
                           and node.name == 'cache_input')
        reads = [node for node in ast.walk(fingerprint)
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == 'stream.read']
        self.assertEqual([ast.unparse(node.args[0]) for node in reads],
                         ['min(remaining, CHECK_INPUT_HASH_CHUNK_BYTES)', '1'])
        logs = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'logs')
        evidence_bound = next(node for node in ast.walk(logs)
                              if isinstance(node, ast.Compare)
                              and ast.unparse(node.left) == 'path.stat().st_size')
        self.assertEqual(ast.unparse(evidence_bound.comparators[0]),
                         'context.config.limits.output_bytes * COMMAND_EVIDENCE_OUTPUT_MULTIPLIER + COMMAND_EVIDENCE_ENVELOPE_BYTES')
        for name in ('CHECK_INPUT_HASH_CHUNK_BYTES', 'COMMAND_EVIDENCE_OUTPUT_MULTIPLIER',
                     'COMMAND_EVIDENCE_ENVELOPE_BYTES'):
            self.assertEqual(DECLARATIONS['check_jobs.py'][name][1], 'safety')

    def test_check_job_slice_and_preview_bounds_use_named_declarations(self):
        tree = ast.parse((ROOT / 'check_jobs.py').read_text(encoding='utf-8'))
        numeric_slices = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Slice):
                continue
            bounds = (node.lower, node.upper, node.step)
            if any(isinstance(child, ast.Constant)
                   and type(child.value) in (int, float)
                   for bound in bounds if bound is not None
                   for child in ast.walk(bound)):
                numeric_slices.append((node.lineno, ast.unparse(node)))
        self.assertEqual(numeric_slices, [])

        preview_widths = {
            ast.unparse(call.args[1])
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and callee_name(call.func) == 'text_preview'
            and len(call.args) >= 2
        }
        self.assertEqual(preview_widths, {
            'MAX_CHECK_JOB_SHORT_PREVIEW_CHARS',
            'MAX_CHECK_JOB_TEXT_PREVIEW_CHARS',
            'MAX_PRIOR_ROLE_REPORT_PREVIEW_CHARS',
        })
        evidence_id = next(node for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name)
                                   and target.id == '_COMMAND_EVIDENCE_ID'
                                   for target in node.targets))
        self.assertIn('MAX_COMMAND_EVIDENCE_ID_HEX_CHARS', ast.unparse(evidence_id.value))
        for name in (
                'MAX_CHECK_JOB_ROLE_ID_SUFFIX_CHARS',
                'MAX_CHECK_JOB_PROJECTION_ROWS',
                'MAX_CHECK_JOB_COMMAND_PREVIEW_ITEMS',
                'MAX_CHECK_JOB_SHORT_PREVIEW_CHARS',
                'MAX_CHECK_JOB_TEXT_PREVIEW_CHARS',
                'MAX_CHECK_CAPTURE_ERROR_BYTES',
                'MAX_COMMAND_EVIDENCE_ID_HEX_CHARS',
                'MAX_COMMAND_EVIDENCE_LIST_ROWS',
                'MAX_PRIOR_ROLE_REPORT_PREVIEW_CHARS'):
            self.assertIn(name, DECLARATIONS['check_jobs.py'])
            self.assertIn(name, {node.id for node in ast.walk(tree)
                                 if isinstance(node, ast.Name)})

    def test_bridge_proxy_fallback_and_chunk_bounds_use_shared_declarations(self):
        for filename, function_name in (('bridge.py', 'connect'), ('mcp_proxy.py', 'forward')):
            with self.subTest(file=filename):
                tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
                functions = [node for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and node.name == function_name]
                self.assertEqual(len(functions), 1)
                defaults = [node for node in ast.walk(functions[0])
                            if isinstance(node, ast.Call)
                            and callee_name(node.func) == 'config.get'
                            and node.args and isinstance(node.args[0], ast.Constant)
                            and node.args[0].value == 'timeout_ms']
                self.assertEqual(len(defaults), 1)
                self.assertEqual(ast.unparse(defaults[0].args[1]), 'DEFAULT_BRIDGE_TIMEOUT_MS')

        tree = ast.parse((ROOT / 'mcp_proxy.py').read_text(encoding='utf-8'))
        forward = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef) and node.name == 'forward')
        reads = [node for node in ast.walk(forward)
                 if isinstance(node, ast.Call) and callee_name(node.func) == 'client.recv']
        self.assertEqual(len(reads), 1)
        self.assertEqual(ast.unparse(reads[0].args[0]), 'MCP_PROXY_RECV_CHUNK_BYTES')

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

    def test_runtime_reexports_are_not_second_declarations(self):
        evidence = ast.parse((ROOT / 'evidence_records.py').read_text(encoding='utf-8'))
        reads = ast.parse((ROOT / 'workspace_reads.py').read_text(encoding='utf-8'))
        runtime = ast.parse((ROOT / 'runtime.py').read_text(encoding='utf-8'))
        self.assertEqual(inventory(evidence), {'MAX_STARTED_EVIDENCE_BYTES': '65536'})
        self.assertEqual(inventory(reads), {'MAX_READ_IDENTITIES': '1024'})
        self.assertNotIn('MAX_STARTED_EVIDENCE_BYTES', inventory(runtime))
        self.assertNotIn('MAX_READ_IDENTITIES', inventory(runtime))

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
        found = inventory(tree, symbolic_bounds=SYMBOLIC_BOUNDS.get('config.py'))
        declared = set(CONFIG_MODULE_BOUNDS)
        for special in (CHECK_RETENTION, HISTORY_TEMPORARIES, DECISION_PROJECTION,
                        DIAGNOSTIC_SAMPLES, DECISION_RECONCILIATION, SMOKE_RETENTION_SCAN):
            declared.update(name for name in special if name in found)
        self.assertEqual(set(found), declared)
        categories = {'mechanism', 'format', 'safety', 'configured policy'}
        for name, (expression, category, rationale) in CONFIG_MODULE_BOUNDS.items():
            with self.subTest(bound=name):
                self.assertEqual(found[name], expression)
                self.assertIn(category, categories)
                self.assertTrue(rationale.strip())

    def test_snapshot_retention_uses_the_declared_day_conversion(self):
        tree = ast.parse((ROOT / "storage.py").read_text(encoding="utf-8"))
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_automatic_snapshot_gc")
        cutoff = next(node.value for node in ast.walk(function)
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "cutoff"
                              for target in node.targets))
        self.assertEqual(ast.unparse(cutoff), "instant - days * _DAY_SECONDS")

    def test_mib_byte_conversions_use_the_shared_unit(self):
        fields = {"free_disk_mb", "memory_mb", "file_mb"}
        counts = {field: 0 for field in fields}
        for filename in ("storage.py", "probes.py", "doctor.py", "check_jobs.py",
                         "runtime.py", "sandbox.py"):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
                    continue
                mentioned = {part.attr for part in ast.walk(node)
                             if isinstance(part, ast.Attribute) and part.attr in fields}
                if not mentioned:
                    continue
                self.assertEqual(len(mentioned), 1)
                self.assertIn("MEBIBYTE_BYTES", {
                    part.id for part in ast.walk(node) if isinstance(part, ast.Name)})
                counts[next(iter(mentioned))] += 1
        self.assertEqual(counts, {"free_disk_mb": 6, "memory_mb": 1, "file_mb": 2})

    def test_systemd_service_settings_are_configured_policy(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        settings = next(node for node in tree.body
                        if isinstance(node, ast.ClassDef) and node.name == 'ServiceSettings')
        fields = {node.target.id: ast.unparse(node.value) for node in settings.body
                  if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
        expected = {
            'systemd_restart_delay_seconds': (
                '15', 'configured policy',
                'services / systemd deployment restart delay default'),
            'systemd_stop_timeout_seconds': (
                '45', 'configured policy',
                'services / systemd deployment graceful-stop timeout default'),
            'systemd_verify_timeout_seconds': (
                '60', 'configured policy',
                'services / systemd-analyze verification wall-time default'),
        }
        self.assertEqual(set(fields), set(expected))
        for name, (default, category, rationale) in expected.items():
            with self.subTest(setting=name):
                self.assertEqual(fields[name], default)
                self.assertEqual(category, 'configured policy')
                self.assertTrue(rationale.strip())

    def test_sandbox_cleanup_timeouts_are_configured_policy(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        settings = next(node for node in tree.body
                        if isinstance(node, ast.ClassDef) and node.name == 'Sandbox')
        fields = {node.target.id: ast.unparse(node.value) for node in settings.body
                  if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
        expected = {
            'cleanup_timeout_seconds': (
                '15', 'configured policy',
                'sandbox / OCI listing, exact-name, post-command and MCP-container cleanup deadline'),
            'bulk_cleanup_timeout_seconds': (
                '30', 'configured policy',
                'sandbox / per-OCI-call project-wide removal deadline'),
            'checkpoint_inspect_timeout_seconds': (
                '15', 'configured policy',
                'sandbox / host-side checkpoint-image preflight inspection deadline'),
        }
        for name, (default, category, rationale) in expected.items():
            with self.subTest(setting=name):
                self.assertEqual(fields[name], default)
                self.assertEqual(category, 'configured policy')
                self.assertTrue(rationale.strip())

        sandbox_tree = ast.parse((ROOT / 'sandbox.py').read_text(encoding='utf-8'))
        inspect = next(node for node in ast.walk(sandbox_tree)
                       if isinstance(node, ast.FunctionDef)
                       and node.name == 'assert_no_checkpoint')
        inspect_run = next(node for node in ast.walk(inspect)
                           if isinstance(node, ast.Call) and callee_name(node.func) == 'run')
        inspect_timeout = next(keyword.value for keyword in inspect_run.keywords
                               if keyword.arg == 'timeout')
        self.assertEqual(ast.unparse(inspect_timeout),
                         'config.sandbox.checkpoint_inspect_timeout_seconds')

        for filename, function_name, expected_runs, expected_timeout in (
                ('engine_config.py', 'stop_engine_containers', 1,
                 'context.config.sandbox.cleanup_timeout_seconds'),
                ('persistent_exec.py', 'ExecSession._remove', 1,
                 'sb.config.sandbox.cleanup_timeout_seconds')):
            with self.subTest(cleanup=filename):
                cleanup_tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
                cleanup = next(node for node in ast.walk(cleanup_tree)
                               if isinstance(node, ast.FunctionDef)
                               and node.name == function_name.rsplit('.', 1)[-1])
                runs = [node for node in ast.walk(cleanup)
                        if isinstance(node, ast.Call) and callee_name(node.func) == 'run']
                self.assertEqual(len(runs), expected_runs)
                for call in runs:
                    timeout = next(keyword.value for keyword in call.keywords
                                   if keyword.arg == 'timeout')
                    self.assertEqual(ast.unparse(timeout), expected_timeout)

    def test_vcs_artifact_page_budget_is_configured_policy(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'artifact_page_bytes')
        expression, category, rationale = (
            "settings.get('artifact_page_bytes', min(524288, max(1, settings.get('max_bytes', 524288) // 8)))",
            'configured policy', 'VCS artifacts / per-page byte budget scaled to invocation cap')
        self.assertEqual(ast.unparse(function.body[-1].value), expression)
        self.assertEqual(category, 'configured policy')
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
        self.assertEqual(policy['DEFAULT_VCS_POLL_INTERVAL_SECONDS'], '300')
        self.assertEqual(defaults['poll_interval_seconds'], 'DEFAULT_VCS_POLL_INTERVAL_SECONDS')
        observation = inventory(ast.parse((ROOT / 'ci_observation.py').read_text(encoding='utf-8')),
                                symbolic_bounds=SYMBOLIC_BOUNDS['ci_observation.py'])
        for constant, setting in (
                ('MAX_CI_FAILURE_BRANCHES', 'ci_cache_max_branches'),
                ('MAX_CI_CHECKS_PER_BRANCH', 'ci_cache_max_checks_per_branch')):
            self.assertEqual(observation[constant], defaults[setting])
            self.assertEqual(DECLARATIONS['ci_observation.py'][constant][1], 'configured policy')
        self.assertEqual(observation['MAX_CI_WAKE_ROWS'], 'MAX_CI_CACHE_ROWS')
        self.assertEqual(CONFIG_MODULE_BOUNDS['MAX_CI_CACHE_ROWS'][:2], ('2048', 'safety'))


    def test_lock_timeout_is_configured_policy(self):
        from mizu.config import Limits, MAX_WAIT_SECONDS, _LIMIT_RANGES
        from mizu import platform
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'lock_timeout_seconds')
        self.assertEqual(ast.unparse(field.value), 'LOCK_TIMEOUT')
        self.assertEqual(Limits().lock_timeout_seconds, platform.LOCK_TIMEOUT)
        self.assertEqual(_LIMIT_RANGES['lock_timeout_seconds'], (0, MAX_WAIT_SECONDS))
        self.assertEqual(DECLARATIONS['platform.py']['LOCK_TIMEOUT'][1], 'configured policy')
        self.assertEqual(DECLARATIONS['fs.py']['_LOCAL_BLOCKING_TIMEOUT'][1], 'configured policy')
        self.assertEqual(Limits().lock_retry_interval_ms, platform.LOCK_RETRY_INTERVAL_MS)
        self.assertEqual(_LIMIT_RANGES['lock_retry_interval_ms'], (1, MAX_WAIT_SECONDS * 1000))
        self.assertEqual(DECLARATIONS['platform.py']['LOCK_RETRY_INTERVAL_MS'][1], 'configured policy')

    def test_pipe_poll_is_configured_policy(self):
        from mizu.config import Limits, _LIMIT_RANGES
        from mizu import platform
        self.assertEqual(Limits().pipe_poll_interval_ms, platform.PIPE_POLL_INTERVAL_MS)
        self.assertEqual(_LIMIT_RANGES['pipe_poll_interval_ms'], (1, 500))
        self.assertEqual(DECLARATIONS['platform.py']['PIPE_POLL_INTERVAL_MS'][1], 'configured policy')

    def test_selection_timer_ceiling_is_configured_policy(self):
        from mizu.config import Limits, TOML_INTEGER_MAX, _LIMIT_RANGES
        self.assertEqual(Limits().selection_max_interval_seconds, 31536000)
        self.assertEqual(_LIMIT_RANGES['selection_max_interval_seconds'], (1, TOML_INTEGER_MAX))
        tree = ast.parse((ROOT / 'selection.py').read_text(encoding='utf-8'))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        validation = functions['validate_selectors']
        self.assertEqual(ast.unparse(validation.args.defaults[0]), 'Limits().selection_max_interval_seconds')
        calls = [node for node in ast.walk(validation) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'seconds']
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(ast.unparse(node.args[1]) == 'max_interval_seconds' for node in calls))
        self.assertIn('0 <= interval <= max_interval_seconds', ast.unparse(validation))
        self.assertEqual(ast.unparse(functions['seconds'].args.defaults[0]), 'TOML_INTEGER_MAX')
        config = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        call = next(node for node in ast.walk(config) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == 'validate_selectors')
        self.assertEqual(ast.unparse(call.args[3]), 'limits.selection_max_interval_seconds')

    def test_probe_executable_identity_uses_configured_bound(self):
        from mizu.config import (DEFAULT_PROBE_EXECUTABLE_BYTES, Limits,
                                 TOML_INTEGER_MAX, _LIMIT_RANGES)
        self.assertEqual(DEFAULT_PROBE_EXECUTABLE_BYTES, 256 * 1024 * 1024)
        self.assertEqual(Limits().probe_executable_bytes, DEFAULT_PROBE_EXECUTABLE_BYTES)
        self.assertEqual(_LIMIT_RANGES['probe_executable_bytes'], (1, TOML_INTEGER_MAX))
        self.assertEqual(CONFIG_MODULE_BOUNDS['DEFAULT_PROBE_EXECUTABLE_BYTES'][1],
                         'configured policy')
        tree = ast.parse((ROOT / 'probes.py').read_text(encoding='utf-8'))
        binding = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == 'target_binding')
        checks = [node for node in ast.walk(binding) if isinstance(node, ast.If)]
        self.assertIn(
            'not path.is_file() or path.stat().st_size > config.limits.probe_executable_bytes',
            [ast.unparse(node.test) for node in checks])
        chunk = inventory(tree)['PROBE_EXECUTABLE_HASH_CHUNK_BYTES']
        self.assertEqual(chunk, '65536')
        reads = [node for node in ast.walk(binding)
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == 'stream.read']
        self.assertEqual([ast.unparse(node.args[0]) for node in reads],
                         ['PROBE_EXECUTABLE_HASH_CHUNK_BYTES'])
        self.assertEqual(DECLARATIONS['probes.py']['PROBE_EXECUTABLE_HASH_CHUNK_BYTES'][1],
                         'mechanism')

    def test_recovery_probe_event_stream_uses_its_declared_ceiling(self):
        tree = ast.parse((ROOT / 'probes.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == '_probe_limits')
        values = [keyword.value for node in ast.walk(function)
                  if isinstance(node, ast.Call) and ast.unparse(node.func) == 'dataclasses.replace'
                  for keyword in node.keywords if keyword.arg == 'event_stream_bytes']
        self.assertEqual([ast.unparse(value) for value in values], [
            'min(MAX_PROBE_EVENT_STREAM_BYTES, config.limits.event_stream_bytes)'])
        self.assertEqual(DECLARATIONS['probes.py']['MAX_PROBE_EVENT_STREAM_BYTES'][1], 'safety')

    def test_check_retention_declarations(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'check_retained')
        self.assertEqual(ast.unparse(field.value), CHECK_RETENTION['check_retained'][0])
        self.assertEqual(inventory(tree)['MAX_CHECK_RETAINED'], CHECK_RETENTION['MAX_CHECK_RETAINED'][0])
        self.assertEqual([entry[1] for entry in CHECK_RETENTION.values()], ['configured policy', 'safety'])
        self.assertTrue(all(entry[2].strip() for entry in CHECK_RETENTION.values()))

    def test_project_goal_limit_is_configured_policy(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body
                     if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'project_goal_bytes')
        self.assertEqual(ast.unparse(field.value), PROJECT_GOAL_LIMIT['project_goal_bytes'][0])
        self.assertEqual(PROJECT_GOAL_LIMIT['project_goal_bytes'][1], 'configured policy')
        self.assertTrue(PROJECT_GOAL_LIMIT['project_goal_bytes'][2].strip())

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

    def test_run_retention_scan_budget_declaration(self):
        tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
        limits = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'Limits')
        field = next(node for node in limits.body if isinstance(node, ast.AnnAssign)
                     and node.target.id == 'run_retention_scan_runs')
        expression, category, rationale = RUN_RETENTION_SCAN[field.target.id]
        self.assertEqual(ast.unparse(field.value), expression)
        self.assertEqual(category, 'configured policy')
        self.assertTrue(rationale.strip())
        storage = ast.parse((ROOT / 'storage_runs.py').read_text(encoding='utf-8'))
        self.assertNotIn('MAX_RUNS_SCANNED', {target.id for node in storage.body
                         if isinstance(node, ast.Assign) for target in node.targets
                         if isinstance(target, ast.Name)})

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
    run(timeout=max(0.0, deadline - now))
    read_json(max_bytes=2 ** 10)
    run(timeout_seconds=20)
    other(max_files=3, max_entries=4, max_depth=2)
    page(limit=1)
    run(timeout=LIMIT)

def sibling():
    run(timeout=7)
    bounded({}, 2 ** 10)
    bounded({}, maximum=LIMIT)
'''
        expected = {
            ('outer', 'run', 'timeout', 0): '5',
            ('outer', 'run', 'maximum', 0): '2',
            ('outer', 'run', 'timeout', 1): '6',
            ('outer', 'run', 'timeout', 2): 'max(0.0, deadline - now)',
            ('outer', 'read_json', 'max_bytes', 0): '2 ** 10',
            ('outer', 'run', 'timeout_seconds', 0): '20',
            ('outer', 'other', 'max_files', 0): '3',
            ('outer', 'other', 'max_entries', 0): '4',
            ('outer', 'other', 'max_depth', 0): '2',
            ('outer', 'page', 'limit', 0): '1',
            ('sibling', 'run', 'timeout', 0): '7',
            ('sibling', 'bounded', 'maximum', 0): '2 ** 10',
            ('sibling', 'bounded', 'maximum', 1): 'LIMIT',
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
