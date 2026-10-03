"""Evidence-path regression tests: verification receipts stay regenerable, not docs.

Regenerable machine receipts (seconds, environment versions, timing peaks)
must not be tracked under docs/. docs/ holds human-authored Markdown policy;
evidence is produced on demand into the ignored private-validation/ tree via
each script's --report flag. See docs/VALIDATION.md.
"""
import subprocess
import sys
import unittest
from pathlib import Path

from support import ROOT


class EvidencePathTests(unittest.TestCase):
    def test_no_tracked_validation_receipts_in_docs(self):
        self.assertEqual(sorted((ROOT / 'docs').glob('validation-*.json')), [])

    def test_docs_link_only_regenerable_paths(self):
        for path in sorted((ROOT / 'docs').glob('*.md')):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('](validation-', text, path.name)
        testing = (ROOT / 'docs/testing.md').read_text(encoding='utf-8')
        self.assertNotIn('/private-validation/', testing)

    def test_gitignore_covers_regenerable_evidence(self):
        ignored = (ROOT / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('private-validation/', ignored)
        self.assertIn('docs/validation-*.json', ignored)

    def test_verification_scripts_support_report(self):
        for script in ('scripts/check.py', 'scripts/check-cli.py',
                       'scripts/benchmark.py', 'scripts/check-writer-process.py'):
            result = subprocess.run(
                [sys.executable, str(ROOT / script), '--help'],
                capture_output=True, text=True, timeout=30, cwd=ROOT)
            self.assertEqual(result.returncode, 0, script)
            self.assertIn('--report', result.stdout, script)


if __name__ == '__main__':
    unittest.main()
