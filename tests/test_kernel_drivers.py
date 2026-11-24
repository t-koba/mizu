"""Generic observation drivers: declarations, invocation and queue appends."""
import sys
import tomllib
import unittest
from types import MappingProxyType

import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.kernel import Driver, invoke, parse_drivers, record_observations
from mizu.kernel.queues import QueueStore, parse_queues

QUEUES = tomllib.loads('''
[queues.observations]
write = ["host"]
read = ["role:alpha", "host"]
[queues.observations.schema]
body = {type = "string", max = 64, required = true}

[queues.restricted]
write = ["role:alpha"]
read = ["role:alpha"]
''')["queues"]

BASE = {"command": ["python", "-c", "pass"], "observation_queue": "observations"}

ECHO = (
    "import json, sys\n"
    "request = json.loads(sys.stdin.readline())\n"
    "print(json.dumps({'echo': request,\n"
    "                  'observations': [{'value': request.get('value', '')}]}))\n"
)
FAIL = "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n"
MALFORMED = "print('not json')\n"
NON_OBJECT = "print('[1, 2]')\n"
DUPLICATE = "print('{\"a\": 1, \"a\": 2}')\n"
HUGE = "print('x' * 20000)\n"
SLEEP = "import time\ntime.sleep(5)\n"


def declaration(**overrides):
    return {**BASE, **overrides}


class DriverDeclarations(unittest.TestCase):
    def setUp(self):
        self.queues = parse_queues(QUEUES)

    def test_valid_shape_and_defaults(self):
        parsed = parse_drivers({"probe": declaration()}, self.queues)
        self.assertIsInstance(parsed, MappingProxyType)
        driver = parsed["probe"]
        self.assertEqual(driver.name, "probe")
        self.assertEqual(driver.command, ("python", "-c", "pass"))
        self.assertEqual(driver.timeout_seconds, 20.0)
        self.assertEqual(driver.max_bytes, 524288)
        self.assertEqual(driver.observation_queue, "observations")
        self.assertEqual(driver.diagnostic_bytes, 0)
        explicit = parse_drivers({"probe": declaration(
            command=["python"], timeout_seconds=1.5, max_bytes=1024,
            diagnostic_bytes=8)}, self.queues)["probe"]
        self.assertEqual((explicit.timeout_seconds, explicit.max_bytes,
                          explicit.diagnostic_bytes), (1.5, 1024, 8))
        self.assertEqual(parse_drivers({}, self.queues), {})

    def test_fail_closed_declarations(self):
        invalid = [
            3,
            [declaration()],
            {"Bad name": declaration()},
            {"bad name": declaration()},
            {"": declaration()},
            {"probe": 3},
            {"probe": declaration(bogus=1)},
            {"probe": declaration(command=[])},
            {"probe": declaration(command="python")},
            {"probe": declaration(command=["python", ""])},
            {"probe": declaration(command=["python", "a\nb"])},
            {"probe": declaration(command=["python", "a\x00b"])},
            {"probe": declaration(timeout_seconds=0)},
            {"probe": declaration(timeout_seconds=86401)},
            {"probe": declaration(timeout_seconds="20")},
            {"probe": declaration(timeout_seconds=True)},
            {"probe": declaration(timeout_seconds=float("nan"))},
            {"probe": declaration(max_bytes=1023)},
            {"probe": declaration(max_bytes=16777217)},
            {"probe": declaration(max_bytes=1024.5)},
            {"probe": declaration(diagnostic_bytes=-1)},
            {"probe": declaration(diagnostic_bytes=8193)},
            {"probe": declaration(diagnostic_bytes=1.5)},
            {"probe": declaration(diagnostic_bytes=8.0)},
            {"probe": declaration(observation_queue="missing")},
            {"probe": declaration(observation_queue="restricted")},
            {"probe": declaration(observation_queue=3)},
        ]
        for raw in invalid:
            with self.subTest(raw=repr(raw)), self.assertRaises(ConfigError):
                parse_drivers(raw, self.queues)


class ConfigWiring(Fixture):
    def test_config_carries_parsed_drivers(self):
        from dataclasses import fields, replace
        self.assertIn("drivers", {field.name for field in fields(type(self.config))})
        self.assertEqual(dict(self.config.drivers), {})
        parsed = parse_drivers({"probe": declaration(observation_queue="notices")},
                               self.config.queues)
        config = replace(self.config, drivers=parsed)
        self.assertEqual(config.drivers["probe"].observation_queue, "notices")
        self.assertEqual(config.drivers["probe"].command, ("python", "-c", "pass"))

    def test_load_parses_and_validates_driver_tables(self):
        original = self.file.read_text()
        self.file.write_text(original + '\n[drivers.probe]\n'
                             'command = ["python", "-c", "pass"]\n'
                             'observation_queue = "notices"\n'
                             'timeout_seconds = 7\nmax_bytes = 4096\n'
                             'diagnostic_bytes = 16\n')
        driver = load(self.file).drivers["probe"]
        self.assertEqual(driver.name, "probe")
        self.assertEqual(driver.command, ("python", "-c", "pass"))
        self.assertEqual((driver.timeout_seconds, driver.max_bytes,
                          driver.diagnostic_bytes), (7.0, 4096, 16))
        self.assertEqual(driver.observation_queue, "notices")
        self.file.write_text(original + '\n[drivers.probe]\n'
                             'command = ["python", "-c", "pass"]\n'
                             'observation_queue = "missing"\n')
        with self.assertRaisesRegex(ConfigError, "observation_queue"):
            load(self.file)
        self.file.write_text(original + '\n[drivers]\n')
        self.assertEqual(dict(load(self.file).drivers), {})


class Invocation(Fixture):
    def write_script(self, name, source):
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return (sys.executable, str(path))

    def driver(self, argv, *, timeout=20.0, maximum=8192, diagnostic=0):
        return Driver("probe", tuple(argv), timeout, maximum, "observations", diagnostic)

    def test_success_returns_parsed_object(self):
        response = invoke(self.driver(self.write_script("echo.py", ECHO)), {"value": "hi"})
        self.assertEqual(response["echo"], {"value": "hi"})
        self.assertEqual(response["observations"], [{"value": "hi"}])

    def test_request_shape_and_bounds(self):
        argv = self.write_script("echo.py", ECHO)
        with self.assertRaises(Denied):
            invoke(self.driver(argv), ["not a dict"])
        with self.assertRaises(Denied):
            invoke(self.driver(argv), {"value": "a\x00b"})
        with self.assertRaises(Denied):
            invoke(self.driver(argv, maximum=1024), {"value": "x" * 2048})

    def test_nonzero_exit_carries_bounded_diagnostic(self):
        argv = self.write_script("fail.py", FAIL)
        with self.assertRaises(Denied) as caught:
            invoke(self.driver(argv, diagnostic=16), {})
        self.assertIn("boom", str(caught.exception))
        self.assertIn("external-untrusted", str(caught.exception))

    def test_bad_responses_refuse(self):
        for name, source in (("malformed.py", MALFORMED), ("nonobject.py", NON_OBJECT),
                             ("duplicate.py", DUPLICATE)):
            with self.subTest(name=name), self.assertRaises(Denied):
                invoke(self.driver(self.write_script(name, source)), {})

    def test_output_limit_cancelling_and_timeout_refuse(self):
        huge = self.driver(self.write_script("huge.py", HUGE), maximum=1024)
        with self.assertRaises(Denied):
            invoke(huge, {})
        echo = self.driver(self.write_script("echo.py", ECHO))
        with self.assertRaises(Denied):
            invoke(echo, {}, cancel=lambda: True)
        sleeping = self.driver(self.write_script("sleep.py", SLEEP), timeout=0.2)
        with self.assertRaises(Denied):
            invoke(sleeping, {})


class Recording(Fixture):
    def setUp(self):
        super().setUp()
        self.store = QueueStore(self.root / "queue-data", parse_queues(QUEUES))
        self.driver = Driver("probe", (sys.executable, "-c", "pass"), 20.0, 8192,
                             "observations", 0)

    def test_append_then_read_as_authorized_reader(self):
        receipt = record_observations(self.store, self.driver, {
            "observations": [{"body": "one"}, {"body": "two"}]})
        self.assertEqual(receipt, {"appended": 2})
        page = self.store.read("observations", "role:alpha")
        self.assertEqual([row["body"]["body"] for row in page["messages"]], ["one", "two"])
        self.assertEqual([row["author"] for row in page["messages"]], ["host", "host"])
        self.assertEqual((page["next"], page["head"]), (2, 2))

    def test_bad_observations_fail_closed(self):
        bad = [
            ["not a dict"],
            {"observations": {"body": "one"}},
            {"observations": [42]},
            {"observations": [{"body": "x" * 100}]},
            {"observations": [{"bogus": "one"}]},
            {"observations": [{"body": "one"}] * 1025},
            {"other": []},
        ]
        for response in bad:
            with self.subTest(response=repr(response)[:60]), self.assertRaises(Denied):
                record_observations(self.store, self.driver, response)
        self.assertEqual(self.store.read("observations", "role:alpha")["head"], 0)


if __name__ == "__main__":
    unittest.main()
