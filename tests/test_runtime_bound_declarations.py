"""First runtime-bound inventory, not a general numeric-literal linter.

Only named, direct module-level Assign/AnnAssign declarations in the five
files below are covered, using the bounded candidate vocabulary below. Numeric
literal/arithmetic expressions are inventoried without evaluating source. This
does not lint arbitrary numeric literals, aliases, nested declarations or other
modules. Expand coverage as the behavior-based inventory reaches other owners.
Classification records current ownership, not approval to leave policy in code;
safety ceilings here are defensive read bounds, not intrinsic format limits.
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
        'MAX_RUNS_SCANNED': ('5000', 'policy', 'usage / history scan work budget'),
        'MAX_RECENT_ENTRIES': ('200', 'policy', 'usage / recent-entry output sample'),
        'MAX_UNKNOWN_KEYS': ('20', 'policy', 'usage / unknown-key diagnostic sample'),
        'MAX_CACHE_AFFINITY_ROWS': ('200', 'policy', 'usage / affinity output sample'),
        'MAX_CACHE_AFFINITY_RUNS': ('200', 'policy', 'usage / affinity history window'),
    },
    'workspace_cache.py': {
        'MAX_ENTRIES': ('100000', 'policy', 'workspace cache / metadata scan work budget'),
        'MAX_DEPTH': ('64', 'policy', 'workspace cache / traversal depth budget'),
        'SCAN_SECONDS': ('10', 'policy', 'workspace cache / preflight wall-time budget'),
        'GIT_BYTES': ('8 * 1024 * 1024', 'policy', 'workspace cache / tracked-path output budget'),
    },
    'ci_requests.py': {
        'RECORD_BYTES': ('8192', 'safety', 'CI requests / bounded evidence-record read'),
        'REQUEST_SCAN_LIMIT': ('10000', 'policy', 'CI requests / directory scan work budget'),
        'REQUEST_RECORD_LIMIT': ('1000', 'policy', 'CI requests / record validation budget'),
    },
    'publication.py': {
        'RECORD_BYTES': ('8192', 'safety', 'publication / bounded evidence-record read'),
        'LINEAGE_JSON_BYTES': ('4096', 'safety', 'publication / bounded caller-supplied lineage JSON'),
        'RECEIPT_SCAN_LIMIT': ('10000', 'policy', 'publication / directory scan work budget'),
        'RECEIPT_RECORD_LIMIT': ('1000', 'policy', 'publication / record validation budget'),
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


if __name__ == '__main__':
    unittest.main()
