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
The `lock_diagnostics.py`
constants are fixed journal-line row and numeric-field format bounds; the
opt-in collection is policy, but it introduces no host-cost numeric bound.
Expressions are inventoried without evaluating source. This does not lint
arbitrary numeric literals, aliases, nested declarations, arbitrary call args,
local arithmetic or other modules. Expand coverage as the behavior-based
inventory reaches other owners. The configured slice checks Usage, SchedulerEvidence,
CiRequestEvidence, PublicationPolicy, PublicationEvidence and WorkspaceCache field defaults,
VCS polling fallback defaults, the retained-check Limits field and ceiling, and explicitly
listed raw web and VCS settings; the decision-history recovery ceiling is checked alone,
not as a full work_items.py inventory. Validation is covered
by focused config tests. Classification records current ownership, not
approval to leave policy in code; safety ceilings here are defensive read
bounds, not intrinsic format limits.
"""
import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'src' / 'mizu'
GATE_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
CANDIDATE = re.compile(
    r'(?:MAX_\w+|\w+_(?:LIMIT|CAP|TIMEOUT\w*|SECONDS|DAYS|DEPTH|ENTRIES|BYTES|ROWS|COUNT))\Z'
)
# name: (exact ast.unparse(value), classification, owner / rationale)
DECLARATIONS = {
    'usage.py': {
        'MAX_ATTEMPT_USAGE_KEY_CHARS': ('256', 'safety', 'usage / fixed stored-key label length ceiling'),
    },
    'dispatch_evidence.py': {
        'MAX_TEXT': ('256', 'safety', 'dispatch evidence / fixed persisted text-label length ceiling'),
    },
    'workspace_cache.py': {},
    'ci_requests.py': {
        'RECORD_BYTES': ('8192', 'safety', 'CI requests / bounded evidence-record read'),
    },
    'publication.py': {
        'RECORD_BYTES': ('8192', 'safety', 'publication / bounded evidence-record read'),
        'LINEAGE_JSON_BYTES': ('4096', 'safety', 'publication / bounded caller-supplied lineage JSON'),
    },
    'run_health.py': {
        'MAX_RATE_FILE_BYTES': ('64 * 1024', 'safety', 'run health / bounded untrusted result-record read'),
        'MAX_RATE_BYTES': ('8 * 1024 * 1024', 'safety', 'run health / total status-rate content-read ceiling'),
    },
    'web.py': {
        'SEARCH_RESULT_LIMIT': ('1000', 'mechanism', 'web search / retained-result ceiling shared with the tool page bound'),
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
    'vcs.py': {
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
        'MAX_PROJECTS': ('5000', 'policy', 'smoke retention / project scan budget'),
        'MAX_PAIRS': ('5000', 'policy', 'smoke retention / pair scan budget'),
        'MAX_REFS': ('20000', 'policy', 'smoke retention / reference scan budget'),
        'MAX_METADATA': ('64 * 1024 * 1024', 'policy', 'smoke retention / cumulative metadata byte budget'),
        'MAX_RECORD': ('128 * 1024', 'safety', 'smoke retention / bounded metadata-record read'),
        'MAX_ENTRIES': ('100000', 'policy', 'smoke retention / metadata entry budget'),
        'MAX_DAYS': ('36500', 'policy', 'smoke retention / allowed retention-age range'),
        'AUDIT_LIMIT': ('128', 'policy', 'smoke retention / retained audit-event count'),
        '_AUTOMATIC_SMOKE_RETENTION_STATE_BYTES': (
            '4096', 'safety', 'smoke retention / bounded automatic due-state read'),
    },
}


# The validation gate is a separate owner. Its enforced host-cost choices have
# configuration owners: the suite child, shared phase budget, per-check ceiling
# and provisioning bound each have a validated option. The two modelled-path
# reserves are fixed conservative safety margins, not command bounds, and
# output/artifact bounds are fixed safety ceilings.
GATE_DECLARATIONS = {
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
# default is configured policy; the ceiling bounds parsed evidence held on the
# host rather than a transport frame.
DECISION_RECONCILIATION = {
    'decision_reconciliation_bytes': (
        'DEFAULT_DECISION_RECONCILIATION_BYTES', 'configured policy',
        'Limits / serialized activation plan and post-map report bytes'),
    'DEFAULT_DECISION_RECONCILIATION_BYTES': (
        '8 * 1024 * 1024', 'configured policy',
        'config / activation plan/report default for several thousand routed rows'),
    'MAX_DECISION_RECONCILIATION_BYTES': (
        '64 * 1024 * 1024', 'safety',
        'config / bounded parsed host evidence for the local activation plan/report'),
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
}


def numeric(node):
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float)
    if isinstance(node, ast.UnaryOp):
        return numeric(node.operand)
    if isinstance(node, ast.BinOp):
        return numeric(node.left) and numeric(node.right)
    return False


def inventory(tree):
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if (isinstance(target, ast.Name) and CANDIDATE.fullmatch(target.id)
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
                self.assertEqual(inventory(tree), {
                    name: entry[0] for name, entry in declarations.items()
                })

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

    def test_vcs_poll_fallbacks_match_configured_policy_defaults(self):
        policy = inventory(ast.parse((ROOT / 'vcs_poll_policy.py').read_text(encoding='utf-8')))
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
        expected.update(_PRIVATE_BYTES='-8', FIRST_CAP='3', SECOND_CAP='3')
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
