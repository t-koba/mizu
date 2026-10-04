"""M1 step 2: [vcs] trusted-adapter invocation contract. Offline only."""
import json
import sys
import unittest

from support import Fixture
from mizu import vcs
from mizu.errors import Denied


def settings(command, timeout=5, maximum=524288):
    return {"command": command, "timeout_seconds": timeout, "max_bytes": maximum}


class VcsAdapterTests(Fixture):
    def test_success_returns_object(self):
        code = "import sys,json; json.load(sys.stdin); print(json.dumps({'refs': {}}))"
        out = vcs.invoke(settings([sys.executable, "-c", code]), {"op": "fetch"})
        self.assertEqual(out, {"refs": {}})

    def test_fetch_refs_validates_shape(self):
        good = "import sys,json; json.load(sys.stdin); print(json.dumps({'refs': {'main': '%s'}}))" % ("a" * 40)
        self.assertEqual(vcs.fetch_refs(settings([sys.executable, "-c", good]))["refs"], {"main": "a" * 40})
        bad = "import sys,json; json.load(sys.stdin); print(json.dumps({'refs': 'bad'}))"
        with self.assertRaises(Denied):
            vcs.fetch_refs(settings([sys.executable, "-c", bad]))
        wrong = "import sys,json; json.load(sys.stdin); print(json.dumps([1,2]))"
        with self.assertRaises(Denied):
            vcs.invoke(settings([sys.executable, "-c", wrong]), {"op": "fetch"})

    def test_nonzero_exit_refused(self):
        code = "import sys; sys.exit(3)"
        with self.assertRaises(Denied):
            vcs.invoke(settings([sys.executable, "-c", code]), {"op": "fetch"})

    def test_malformed_json_refused(self):
        code = "import sys; sys.stdout.write('not-json\\n')"
        with self.assertRaises(Denied):
            vcs.invoke(settings([sys.executable, "-c", code]), {"op": "fetch"})

    def test_timeout_refused(self):
        code = "import time; time.sleep(30)"
        with self.assertRaises(Denied):
            vcs.invoke(settings([sys.executable, "-c", code], timeout=1), {"op": "fetch"})

    def test_oversize_response_refused(self):
        code = "import sys; sys.stdout.write('x' * 100000)"
        with self.assertRaises(Denied):
            vcs.invoke(settings([sys.executable, "-c", code], maximum=1024), {"op": "fetch"})

    def test_unconfigured_refused(self):
        with self.assertRaises(Denied):
            vcs.invoke(settings([]), {"op": "fetch"})

    def test_bad_request_refused_without_spawn(self):
        with self.assertRaises(Denied):
            vcs.invoke(settings([sys.executable, "-c", "pass"]), {"op": ""})
        with self.assertRaises(Denied):
            vcs.invoke(settings([sys.executable, "-c", "pass"], maximum=8), {"op": "fetch", "extra": "x" * 100})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
