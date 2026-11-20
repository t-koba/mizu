"""Keep the local filesystem benchmark usable as an end-to-end tool."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BenchmarkSmokeTests(unittest.TestCase):
    def test_minimal_benchmark_restores_synthetic_records(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "benchmark.py"),
             "--files", "1", "--bytes-per-file", "128", "--records", "10"],
            cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["kind"], "local-filesystem-and-evidence-benchmark")
        self.assertIn("restore", receipt["measurements"])
        self.assertEqual(receipt["objects_after_two_captures"], 1)


if __name__ == "__main__":
    unittest.main()
