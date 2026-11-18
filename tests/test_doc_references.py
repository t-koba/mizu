"""Documentation citations to tests must resolve to real modules, classes and methods.

Docs cite tests as evidence (``tests/test_x.py::Class.test_y``) and sometimes
name only the module. A rename can silently rot those citations; this guard
checks them without importing test modules (AST only, no fixture side effects).
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Explicit test citations. Module-only references are checked for existence;
# fully-qualified ones additionally check the class and method are defined.
REFERENCE = re.compile(r"tests/(test_\w+)\.py(?:::(\w+)\.(\w+))?")


def documents():
    paths = sorted((ROOT / "docs").glob("*.md")) + sorted(ROOT.glob("*.md"))
    paths += sorted((ROOT / "policies").glob("*.md"))
    return [p for p in paths if p.is_file()]


class DocReferenceTests(unittest.TestCase):
    def test_test_citations_resolve(self):
        modules = {}
        for doc in documents():
            for match in REFERENCE.finditer(doc.read_text(encoding="utf-8")):
                name, cls, method = match.groups()
                path = ROOT / "tests" / f"{name}.py"
                self.assertTrue(path.is_file(),
                                f"{doc.name}: missing test module {path.name}")
                if cls is None:
                    continue
                tree = modules.setdefault(name, ast.parse(
                    path.read_text(encoding="utf-8"), filename=str(path)))
                classes = {
                    node.name: {item.name for item in node.body
                                if isinstance(item, ast.FunctionDef)}
                    for node in tree.body if isinstance(node, ast.ClassDef)
                }
                self.assertIn(cls, classes,
                              f"{doc.name}: {path.name} has no class {cls}")
                self.assertIn(method, classes[cls],
                              f"{doc.name}: {path.name}::{cls} has no method {method}")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
