"""Declared persistent directories: identity facts, mounts, liveness and config."""
import copy
import dataclasses
import tomllib
import unittest

import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, digest
from mizu.kernel import (MountRule, PersistentDirectory, identity_for,
                         mount_mode, parse_persistent, unreferenced)

DECLARATIONS = tomllib.loads('''
[persistent.artifact]
identity_from = ["tier", "stage"]
mounts = [
    {mode = "ro", when = {tier = "shared"}},
    {mode = "rw", when = {tier = "local", stage = "active"}},
]

[persistent.fixed]
identity_from = ["tier"]
identity_value = "fixed-identity"
mounts = [{mode = "ro", when = {tier = "shared"}}]
''')["persistent"]


class PersistentDeclarations(unittest.TestCase):
    def test_valid_declarations_are_immutable(self):
        declarations = parse_persistent(DECLARATIONS)
        self.assertEqual(set(declarations), {"artifact", "fixed"})
        artifact = declarations["artifact"]
        self.assertEqual(artifact.name, "artifact")
        self.assertEqual(artifact.identity_from, ("tier", "stage"))
        self.assertIsNone(artifact.identity_value)
        self.assertEqual(tuple(rule.mode for rule in artifact.mounts), ("ro", "rw"))
        self.assertEqual(dict(artifact.mounts[0].when), {"tier": "shared"})
        self.assertEqual(dict(artifact.mounts[1].when),
                         {"tier": "local", "stage": "active"})
        self.assertEqual(declarations["fixed"].identity_value, "fixed-identity")
        with self.assertRaises(TypeError):
            declarations["artifact"] = artifact
        with self.assertRaises(TypeError):
            artifact.mounts[0].when["tier"] = "other"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.name = "other"

    def test_invalid_declarations_fail_closed(self):
        bad_mode = copy.deepcopy(DECLARATIONS)
        bad_mode["artifact"]["mounts"][0]["mode"] = "link"
        bad_identity = copy.deepcopy(DECLARATIONS)
        bad_identity["artifact"]["identity_from"] = ["tier", ""]
        bad_when = copy.deepcopy(DECLARATIONS)
        bad_when["artifact"]["mounts"][0]["when"] = ["tier"]
        bad_value = copy.deepcopy(DECLARATIONS)
        bad_value["artifact"]["identity_value"] = "line\nbreak"
        unknown = copy.deepcopy(DECLARATIONS)
        unknown["artifact"]["extra"] = 1
        cases = [
            (None, "persistent must be a table"),
            (unknown, "Unknown keys"),
            (bad_mode, "mode must be"),
            (bad_identity, "identity_from"),
            (bad_when, "when must be a table"),
            (bad_value, "identity_value"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(ConfigError, message):
                parse_persistent(raw)

    def test_one_bad_declaration_refuses_the_whole_set(self):
        raw = copy.deepcopy(DECLARATIONS)
        raw["broken"] = {"identity_from": ["tier"], "mounts": [{"mode": "link", "when": {}}]}
        with self.assertRaisesRegex(ConfigError, "persistent.broken.mounts"):
            parse_persistent(raw)

    def test_bounds_are_exact(self):
        declaration = {"identity_from": ["tier"], "mounts": []}
        self.assertEqual(len(parse_persistent({f"dir{n}": declaration for n in range(64)})), 64)
        with self.assertRaisesRegex(ConfigError, "more than 64"):
            parse_persistent({f"dir{n}": declaration for n in range(65)})
        many = {"identity_from": [f"fact{n}" for n in range(33)], "mounts": []}
        with self.assertRaisesRegex(ConfigError, "at most 32"):
            parse_persistent({"artifact": many})
        long_value = {"identity_from": ["tier"], "identity_value": "x" * 256, "mounts": []}
        with self.assertRaisesRegex(ConfigError, "at most 255"):
            parse_persistent({"artifact": long_value})


class IdentityFacts(unittest.TestCase):
    def setUp(self):
        self.declaration = parse_persistent(DECLARATIONS)["artifact"]
        self.facts = {"tier": "shared", "stage": "active", "note": "one"}
        self.expected = digest(canonical({"tier": "shared", "stage": "active"}))

    def test_digest_uses_only_selected_facts(self):
        self.assertEqual(identity_for(self.declaration, self.facts), self.expected)
        self.assertEqual(identity_for(self.declaration, {**self.facts, "note": "two"}),
                         self.expected)
        self.assertEqual(identity_for(self.declaration,
                                      {"stage": "active", "tier": "shared"}), self.expected)
        self.assertNotEqual(identity_for(self.declaration,
                                         {**self.facts, "stage": "idle"}), self.expected)
        self.assertEqual(len(self.expected), 64)

    def test_override_returns_configured_literal(self):
        declaration = parse_persistent(DECLARATIONS)["fixed"]
        self.assertEqual(identity_for(declaration, {"tier": "anything"}), "fixed-identity")

    def test_missing_or_malformed_facts_fail_closed(self):
        with self.assertRaisesRegex(Denied, "stage"):
            identity_for(self.declaration, {"tier": "shared"})
        with self.assertRaisesRegex(Denied, "stage"):
            identity_for(self.declaration, {"tier": "shared", "stage": "line\nbreak"})
        with self.assertRaisesRegex(Denied, "facts must be a table"):
            identity_for(self.declaration, None)


class MountRules(unittest.TestCase):
    def setUp(self):
        self.declaration = parse_persistent(DECLARATIONS)["artifact"]

    def test_ordered_first_match_ro_rw_and_none(self):
        self.assertEqual(mount_mode(self.declaration, {"tier": "shared"}), "ro")
        self.assertEqual(mount_mode(self.declaration, {"tier": "local", "stage": "active"}),
                         "rw")
        self.assertIsNone(mount_mode(self.declaration, {"tier": "local", "stage": "idle"}))
        self.assertIsNone(mount_mode(self.declaration, {}))
        overlapping = dataclasses.replace(self.declaration, mounts=(
            self.declaration.mounts[0], MountRule("rw", {"tier": "shared"})))
        self.assertEqual(mount_mode(overlapping, {"tier": "shared", "stage": "active"}), "ro")

    def test_non_mapping_facts_fail_closed(self):
        with self.assertRaisesRegex(Denied, "facts must be a table"):
            mount_mode(self.declaration, ["tier"])


class Liveness(unittest.TestCase):
    def test_shared_identity_lives_while_any_referrer_remains(self):
        shared = ("artifact", "digest-a")
        instances = [shared, ("artifact", "digest-b"), ("fixed", "digest-c")]
        self.assertEqual(unreferenced(instances, [("artifact", "digest-a")]),
                         frozenset({("artifact", "digest-b"), ("fixed", "digest-c")}))
        self.assertEqual(unreferenced(instances, [("fixed", "digest-a")]), frozenset(instances))

    def test_removal_only_without_referrer(self):
        instance = ("artifact", "digest-a")
        self.assertEqual(unreferenced([instance], []), frozenset({instance}))
        self.assertEqual(unreferenced([instance], [instance]), frozenset())

    def test_malformed_pairs_fail_closed(self):
        for bad in ([("artifact",)], [("Artifact", "digest")],
                    [("artifact", "line\nbreak")], ["artifact"], [42], [None]):
            with self.subTest(bad=bad):
                with self.assertRaises(Denied):
                    unreferenced(bad, [])
                with self.assertRaises(Denied):
                    unreferenced([], bad)


class ConfigWiring(Fixture):
    def test_absent_persistent_table_is_empty(self):
        self.assertEqual(dict(self.config.persistent), {})

    def test_declared_table_is_exposed_on_config(self):
        self.file.write_text(self.file.read_text() + '''
[persistent.artifact]
identity_from = ["tier", "stage"]
mounts = [
    {mode = "ro", when = {tier = "shared"}},
    {mode = "rw", when = {tier = "local", stage = "active"}},
]
''')
        configured = load(self.file)
        self.assertEqual(set(configured.persistent), {"artifact"})
        declaration = configured.persistent["artifact"]
        self.assertIsInstance(declaration, PersistentDirectory)
        self.assertEqual(declaration.identity_from, ("tier", "stage"))
        facts = {"tier": "local", "stage": "active"}
        self.assertEqual(identity_for(declaration, facts),
                         digest(canonical({"tier": "local", "stage": "active"})))
        self.assertEqual(mount_mode(declaration, facts), "rw")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
