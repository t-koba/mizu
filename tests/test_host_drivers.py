"""Host invocation seam: configured drivers append observations through the host CLI."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu import cli
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.host_drivers import observe
from mizu.project import Project

ECHO = (
    "import json, sys\n"
    "request = json.loads(sys.stdin.readline())\n"
    "print(json.dumps({'observations': [{'body': request['value']}]}))\n"
)
# The declared notices schema accepts a string 'body' only.
REFUSED = "print(json.dumps({'observations': [{'bogus': 1}]}))\n"


class HostDriverFixture(Fixture):
    def setUp(self):
        super().setUp()
        argv = {}
        for name, source in (("echo", ECHO), ("refused", REFUSED)):
            path = self.root / (name + ".py")
            path.write_text(source, encoding="utf-8")
            argv[name] = [sys.executable, str(path)]
        text = self.file.read_text()
        for name in ("echo", "refused"):
            text += (f"\n[drivers.{name}]\ncommand = {json.dumps(argv[name])}\n"
                     'observation_queue = "notices"\n')
        self.file.write_text(text)
        self.config = load(self.file)
        self.project = Project(self.config, self.project.name)

    def cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "load", return_value=self.config), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(["--config", str(self.file), *args])
            else:
                code = cli.main(["--config", str(self.file), *args])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def page(self):
        return self.project.kernel_queues.read("notices", "role:worker")


class HostObservation(HostDriverFixture):
    def test_observe_appends_to_declared_queue_as_host(self):
        receipt = observe(self.config, self.project, "echo", {"value": "one"})
        self.assertEqual(receipt, {"appended": 1})
        page = self.page()
        self.assertEqual([row["body"] for row in page["messages"]], [{"body": "one"}])
        self.assertEqual([row["author"] for row in page["messages"]], ["host"])
        self.assertEqual((page["head"], page["next"]), (1, 1))

    def test_unknown_driver_and_non_dict_request_fail_closed(self):
        for name in ("absent", None, 3):
            with self.subTest(name=name), self.assertRaises(Denied):
                observe(self.config, self.project, name, {})
        for request in ([1], "text", None):
            with self.subTest(request=request), self.assertRaises(Denied):
                observe(self.config, self.project, "echo", request)
        self.assertEqual(self.page()["head"], 0)

    def test_queue_schema_refusal_is_denied(self):
        with self.assertRaises(Denied):
            observe(self.config, self.project, "refused", {})
        self.assertEqual(self.page()["head"], 0)


class ObserveCli(HostDriverFixture):
    def test_cli_request_argument_and_stdin_append(self):
        code, receipt, _ = self.cli("observe", "sample", "echo", "--request",
                                    json.dumps({"value": "argument"}))
        self.assertEqual((code, receipt), (0, {"appended": 1}))
        code, receipt, _ = self.cli("observe", "sample", "echo", "--request", "-",
                                    stdin=json.dumps({"value": "stdin"}))
        self.assertEqual((code, receipt), (0, {"appended": 1}))
        page = self.page()
        self.assertEqual([row["body"]["body"] for row in page["messages"]], ["argument", "stdin"])
        self.assertEqual([row["author"] for row in page["messages"]], ["host", "host"])
        self.assertEqual(page["head"], 2)

    def test_cli_fail_closed_as_json(self):
        cases = (
            (("observe", "sample", "absent", "--request", "{}"), "Denied"),
            (("observe", "sample", "echo", "--request", "[1]"), "ConfigError"),
            (("observe", "sample", "echo", "--request", "not json"), "ConfigError"),
            (("observe", "sample", "refused", "--request", "{}"), "Denied"),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                code, out, err = self.cli(*args)
                self.assertNotEqual(code, 0)
                self.assertIsNone(out)
                self.assertEqual(json.loads(err)["type"], expected)
        self.assertEqual(self.page()["head"], 0)


if __name__ == "__main__":
    unittest.main()
