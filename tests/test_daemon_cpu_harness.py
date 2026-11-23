"""Focused checks for the local daemon-CPU harness's steady-state gate."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'daemon-cpu.py'


def _harness():
    spec = importlib.util.spec_from_file_location('daemon_cpu_harness', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SteadyStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = _harness()

    @staticmethod
    def _readings(values):
        """Feed per-PID readings in order; the fake clock advances on sleep."""
        readings = iter(values)
        clock = [0.0]

        def pid_cpu(_pid):
            return next(readings)

        def sleep(seconds):
            clock[0] += seconds

        return pid_cpu, sleep, lambda: clock[0]

    def _probe(self, values, procs, **kwargs):
        original = self.harness._pid_cpu
        self.harness._pid_cpu, sleep, clock = self._readings(values)
        try:
            return self.harness._steady_state(procs, sleep=sleep, clock=clock, **kwargs)
        finally:
            self.harness._pid_cpu = original

    def test_waits_for_two_quiet_probes_before_the_window(self):
        procs = [type('Proc', (), {'pid': pid})() for pid in (101, 102)]
        observed, info = self._probe([
            0.0, 0.0,                   # initial availability sample
            0.10, 0.10, 0.30, 0.30,     # busy probe: 0.2 CPU-seconds per child
            0.31, 0.31, 0.315, 0.315,   # quiet probe: 0.005
            0.316, 0.316, 0.318, 0.318,  # quiet probe: 0.002
        ], procs, min_warmup=3.0)
        self.assertTrue(observed)
        self.assertEqual(info['method'], 'proc-per-pid')
        self.assertEqual(info['probes'], [0.2, 0.005, 0.002])
        self.assertEqual(info['observed_warmup_seconds'], 3.75)

    def test_never_quiet_is_bounded_and_not_observed(self):
        procs = [type('Proc', (), {'pid': 101})()]
        observed, info = self._probe(
            [float(n) for n in range(20)], procs,
            min_warmup=3.0, max_settle=1.0)
        self.assertFalse(observed)
        self.assertEqual(info['observed_warmup_seconds'], 4.0)
        self.assertEqual(len(info['probes']), 4)

    def test_no_per_pid_cpu_falls_back_without_a_steady_claim(self):
        procs = [type('Proc', (), {'pid': 101})()]
        observed, info = self._probe([None], procs, min_warmup=3.0)
        self.assertFalse(observed)
        self.assertEqual(info['method'], 'reaped-children-getrusage')
        self.assertEqual(info['observed_warmup_seconds'], 3.0)

    def test_startup_sized_warmup_is_outside_the_accepted_bound(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--warmup', '0.01'],
            cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--warmup must be 0.1..60', result.stderr)


if __name__ == '__main__':
    unittest.main()
