"""First runtime-bound inventory, not a general numeric-literal linter.

The nine files below cover named, direct module-level Assign/AnnAssign bounds
and direct numeric literal/arithmetic values at a short resource-call keyword
set. Expressions are inventoried without evaluating source. This does not lint
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
