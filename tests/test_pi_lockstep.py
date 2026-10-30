"""Both managed Pi adapters resolve one synchronized @earendil-works release."""
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = (("pi", "pi-coding-agent"), ("pi-durable", "pi-durable"))
STABLE_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class PiDependencyLockstepTests(unittest.TestCase):
    def test_manifests_and_lockfiles_resolve_one_stable_version(self):
        versions = set()
        for adapter, required in ADAPTERS:
            directory = ROOT / "adapters" / adapter
            manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"),
                                  object_pairs_hook=_unique)
            lock = json.loads((directory / "package-lock.json").read_text(encoding="utf-8"),
                              object_pairs_hook=_unique)
            dependencies = manifest["dependencies"]
            packages = lock["packages"]
            self.assertEqual(packages[""]["dependencies"], dependencies)
            scoped = {name: version for name, version in dependencies.items()
                      if name.startswith("@earendil-works/")}
            for name in (required, "pi-ai"):
                self.assertIn("@earendil-works/" + name, scoped)
            for name, version in scoped.items():
                self.assertRegex(version, STABLE_VERSION)
                self.assertEqual(packages["node_modules/" + name]["version"], version)
                versions.add(version)
            for path, package in packages.items():
                installed = (path[len("node_modules/"):]
                             if path.startswith("node_modules/")
                             else path.rsplit("/node_modules/", 1)[-1])
                if installed.startswith("@earendil-works/") and installed.count("/") == 1:
                    self.assertRegex(package["version"], STABLE_VERSION)
                    versions.add(package["version"])
        self.assertEqual(len(versions), 1)


if __name__ == '__main__':
    unittest.main()
