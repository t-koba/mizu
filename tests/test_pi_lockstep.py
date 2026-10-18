"""Both managed Pi adapters resolve one synchronized @earendil-works release."""
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ("adapters/pi", "adapters/pi-durable")


class PiDependencyLockstepTests(unittest.TestCase):
    def test_manifests_and_lockfiles_resolve_one_pi_release(self):
        versions = set()
        manifests = {}
        for directory in ADAPTERS:
            root = ROOT / directory
            manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
            lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
            manifests[directory] = manifest
            scoped = {name: version for name, version in manifest["dependencies"].items()
                      if name.startswith("@earendil-works/")}
            self.assertTrue(scoped, f"{directory} has no direct Pi dependencies")
            for name, declared in scoped.items():
                self.assertRegex(declared, r"^\d+\.\d+\.\d+$", name)
                resolved = lock["packages"].get(f"node_modules/{name}")
                self.assertIsInstance(resolved, dict, f"{directory} lock omits {name}")
                self.assertEqual(resolved.get("version"), declared,
                                 f"{directory} lock does not resolve declared {name}")
                versions.add(declared)

            # Include top-level and nested Pi packages, but not ordinary
            # dependencies nested underneath an @earendil-works package.
            for package_path, package in lock["packages"].items():
                installed = package_path.rsplit("/node_modules/", 1)[-1]
                if installed.startswith("@earendil-works/") and installed.count("/") == 1:
                    self.assertIn("version", package, package_path)
                    versions.add(package["version"])

        self.assertEqual(len(versions), 1,
                         f"Pi package versions differ across adapters/locks: {sorted(versions)}")
        release = next(iter(versions))
        self.assertEqual(manifests["adapters/pi"]["dependencies"].get(
            "@earendil-works/pi-coding-agent"), release)
        self.assertEqual(manifests["adapters/pi-durable"]["dependencies"].get(
            "@earendil-works/pi-durable"), release)
        self.assertEqual(manifests["adapters/pi"]["dependencies"].get(
            "@earendil-works/pi-ai"), release)
        self.assertEqual(manifests["adapters/pi-durable"]["dependencies"].get(
            "@earendil-works/pi-ai"), release)


if __name__ == "__main__":
    unittest.main()
