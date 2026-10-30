"""Both managed Pi adapters resolve one synchronized @earendil-works release."""
import unittest
from pathlib import Path

from mizu.pi_release import pinned_version


ROOT = Path(__file__).resolve().parents[1]


class PiDependencyLockstepTests(unittest.TestCase):
    def test_manifests_and_lockfiles_resolve_one_pi_release(self):
        # Shared with the host release check: manifests, root declarations,
        # required packages, and every top-level/nested Pi lock resolution.
        self.assertRegex(pinned_version(ROOT), r'^\d+\.\d+\.\d+$')


if __name__ == '__main__':
    unittest.main()
