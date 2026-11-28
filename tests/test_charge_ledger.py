"""Append-only charge ledger: bounded records, constant-cost writes, sidecar reads."""
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401 -- puts src on sys.path for the standalone unittest run

from mizu.charge_ledger import (
    MAX_COUNTS_BYTES, MAX_IDS, MAX_LINE_BYTES, MAX_PROJECTS,
    MAX_PROJECT_CHARS, MAX_REQUEST_ID_CHARS, READ_PAGE,
    Charge, DayLedger, charge_line, parse_charge_line,
)
from mizu.errors import Denied, InfraExceeded
from mizu.fs import canonical


class ChargeLedgerTests(unittest.TestCase):
    DAY = "2026-01-15"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = DayLedger(self.root, self.DAY)

    def _write_counts(self, value):
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger.counts_path.write_bytes(canonical(value))

    @staticmethod
    def _even(i):
        """Equal-length records: compensate for the ordinal's digit count."""
        digits = len(str(i))
        return Charge("x" * (12 - 2 * digits) + str(i), "alpha", i)

    def _append(self, ledger, count):
        sizes = []
        with ledger.lock():
            for i in range(count):
                before = ledger.log_path.stat().st_size if ledger.log_path.exists() else 0
                ledger.append(self._even(i))
                sizes.append(ledger.log_path.stat().st_size - before)
        return sizes

    def test_charge_line_round_trip_and_bounds(self):
        charge = Charge("req-1", "alpha", 7)
        line = charge_line(charge)
        self.assertEqual(line, canonical({"id": "req-1", "project": "alpha", "seq": 7}))
        self.assertEqual(line.count(b"\n"), 1)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(parse_charge_line(line), charge)
        self.assertEqual(charge_line(parse_charge_line(line)), line)

        wide = Charge("é" * MAX_REQUEST_ID_CHARS, "p" * MAX_PROJECT_CHARS, 42)
        wide_line = charge_line(wide)
        self.assertLessEqual(len(wide_line), MAX_LINE_BYTES)
        self.assertEqual(parse_charge_line(wide_line), wide)

        for bad in (
            Charge("x" * (MAX_REQUEST_ID_CHARS + 1), "alpha", 0),
            Charge("id", "p" * (MAX_PROJECT_CHARS + 1), 0),
            Charge("a\x00b", "alpha", 0), Charge("a\nb", "alpha", 0),
            Charge("id", "a\x00b", 0), Charge("id", "a\nb", 0),
            Charge("id", "alpha", -1), Charge("id", "alpha", True),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(Denied):
                    charge_line(bad)
        with self.assertRaises(Denied):
            charge_line({"id": "x", "project": "alpha", "seq": 0})

    def test_parse_refuses_malformed_noncanonical_and_overbound_lines(self):
        good = charge_line(Charge("id", "alpha", 0))
        cases = (
            b"", b"\n", b"not json\n", good[:-1], good + b"\n",
            b'{"id":"id","project":"alpha","seq":0,"extra":1}\n',
            b'{ "id": "id", "project": "alpha", "seq": 0 }\n',
            b'{"id":"id","project":"alpha","seq":"0"}\n',
            b'{"id":"id","project":"alpha","seq":0.0}\n',
            b"x" * (MAX_LINE_BYTES + 1) + b"\n",
        )
        for line in cases:
            with self.subTest(line=line[:40]):
                with self.assertRaises(Denied):
                    parse_charge_line(line)

    def test_appends_add_constant_bytes_and_pages_through_all_records(self):
        total = 2000
        deltas = self._append(self.ledger, total)
        expected = len(charge_line(self._even(0)))
        self.assertEqual(min(deltas), expected)
        self.assertEqual(max(deltas), expected)
        self.assertEqual(deltas[0], deltas[total - 1])
        self.assertEqual(self.ledger.log_path.stat().st_size, total * expected)

        position = 0
        pages = 0
        seen = 0
        last = 0
        while True:
            page = self.ledger.read_from(position)
            if not page:
                break
            pages += 1
            self.assertLessEqual(len(page), READ_PAGE)
            for charge, next_seq in page:
                self.assertEqual(charge, self._even(charge.seq))
                self.assertEqual(next_seq, charge.seq + 1)
                last = next_seq
                seen += 1
            position = page[-1][1]
        self.assertEqual(seen, total)
        self.assertEqual(pages, math.ceil(total / READ_PAGE))
        self.assertEqual(last, total)
        self.assertEqual(self.ledger.read_from(total), ())
        self.assertEqual(self.ledger.read_from(READ_PAGE)[0][0], self._even(READ_PAGE))

    def test_counts_and_membership_are_sidecar_backed(self):
        names = ("alpha", "beta", "gamma")
        with self.ledger.lock():
            for i in range(6):
                self.ledger.append(Charge(f"id-{i}", names[i % 3], i))
        expected = {"shared": 6, "projects": {"alpha": 2, "beta": 2, "gamma": 2}}
        self.assertEqual(self.ledger.counts(), expected)
        for i in range(6):
            self.assertTrue(self.ledger.has(names[i % 3], f"id-{i}"))
            self.assertFalse(self.ledger.has(names[(i + 1) % 3], f"id-{i}"))
        self.assertFalse(self.ledger.has("delta", "id-0"))

        # Sidecars still answer after the log is made unreadable: no log scan.
        self.ledger.log_path.write_bytes(b"not a charge\n")
        self.assertEqual(self.ledger.counts(), expected)
        self.assertTrue(self.ledger.has("beta", "id-1"))
        with self.assertRaises(Denied):
            self.ledger.read_from(0)

    def test_duplicate_charge_refuses_and_leaves_state_unchanged(self):
        self.ledger.append(Charge("dup", "alpha", 0))
        counts = self.ledger.counts()
        body = self.ledger.log_path.read_bytes()
        with self.assertRaisesRegex(Denied, "Duplicate"):
            self.ledger.append(Charge("dup", "alpha", 1))
        with self.assertRaisesRegex(Denied, "Duplicate"):
            self.ledger.add("alpha", "dup")
        self.assertEqual(self.ledger.counts(), counts)
        self.assertEqual(self.ledger.log_path.read_bytes(), body)
        self.assertTrue(self.ledger.has("alpha", "dup"))

    def test_project_and_id_bounds_fail_closed(self):
        projects = {f"p{i:04d}": 0 for i in range(MAX_PROJECTS)}
        self._write_counts({"shared": 0, "projects": projects})
        self.assertEqual(len(self.ledger.counts()["projects"]), MAX_PROJECTS)
        with self.assertRaisesRegex(InfraExceeded, "project table is full"):
            self.ledger.append(Charge("new-id", "new-project", 0))

        self._write_counts({"shared": MAX_IDS, "projects": {"alpha": MAX_IDS}})
        with self.assertRaisesRegex(InfraExceeded, "id index is full"):
            self.ledger.append(Charge("new-id", "alpha", MAX_IDS))
        with self.assertRaisesRegex(InfraExceeded, "id index is full"):
            self.ledger.add("alpha", "another-id")

    def test_corrupt_sidecars_and_log_lines_fail_closed(self):
        self.ledger.append(Charge("one", "alpha", 0))
        with self.ledger.log_path.open("ab") as stream:
            stream.write(b"damaged line\n")
        with self.assertRaises(Denied):
            self.ledger.read_from(0)
        with self.assertRaises(Denied):
            self.ledger.read_from(1)

        for body in (b"{damaged\n", b"x" * (MAX_COUNTS_BYTES + 1),
                     canonical({"shared": -1, "projects": {}}),
                     canonical({"shared": 1, "projects": {"alpha": 2}})):
            with self.subTest(body=body[:20]):
                self.ledger.counts_path.write_bytes(body)
                with self.assertRaises(Denied):
                    self.ledger.counts()
                with self.assertRaises(Denied):
                    self.ledger.append(Charge("two", "alpha", 1))

        self.ledger.counts_path.unlink()
        self.ledger.index_dir.mkdir(exist_ok=True)
        fresh = self.ledger._key("alpha", "never-added")
        os.symlink(self.ledger.root / "elsewhere", self.ledger.index_dir / fresh)
        with self.assertRaises(Denied):
            self.ledger.has("alpha", "never-added")

    def test_failed_log_append_does_not_poison_the_id(self):
        original = self.ledger._append_log

        def fail(line):
            raise OSError("simulated ENOSPC")

        self.ledger._append_log = fail
        try:
            with self.assertRaises(OSError):
                self.ledger.append(Charge("req-1", "alpha", 0))
        finally:
            self.ledger._append_log = original
        self.assertEqual(self.ledger.counts(), {"shared": 0, "projects": {}})
        self.assertFalse(self.ledger.has("alpha", "req-1"))

        self.ledger.append(Charge("req-1", "alpha", 0))
        self.assertEqual(self.ledger.read_from(0), ((Charge("req-1", "alpha", 0), 1),))
        self.assertEqual(self.ledger.counts(), {"shared": 1, "projects": {"alpha": 1}})

    def test_recover_derives_sidecars_from_durable_but_unindexed_log(self):
        with self.ledger.lock():
            self.ledger.append(Charge("one", "alpha", 0))
            # Crash after the durable log append, before any sidecar write.
            with self.ledger.log_path.open("ab") as stream:
                stream.write(charge_line(Charge("two", "beta", 1)))
            self.assertEqual(self.ledger.counts(), {"shared": 1, "projects": {"alpha": 1}})
            self.assertFalse(self.ledger.has("beta", "two"))

        # A new process recovers under the lock before trusting the sidecars.
        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            self.assertEqual(fresh.recover(), 1)
            self.assertEqual(fresh.recover(), 0)
        expected = {"shared": 2, "projects": {"alpha": 1, "beta": 1}}
        self.assertEqual(fresh.counts(), expected)
        self.assertTrue(fresh.has("alpha", "one"))
        self.assertTrue(fresh.has("beta", "two"))
        self.assertEqual([charge for charge, _ in fresh.read_from(0)],
                         [Charge("one", "alpha", 0), Charge("two", "beta", 1)])

        # A lost checkpoint and key replay from the start without a double charge.
        self.ledger.counts_path.unlink()
        (self.ledger.index_dir / self.ledger._key("beta", "two")).unlink()
        with fresh.lock():
            self.assertEqual(fresh.recover(), 2)
        self.assertEqual(fresh.counts(), expected)
        self.assertTrue(fresh.has("alpha", "one"))
        self.assertTrue(fresh.has("beta", "two"))

        with fresh.lock():
            fresh.append(Charge("three", "alpha", 2))
        self.assertEqual(fresh.counts()["shared"], 3)
        body = fresh.log_path.read_bytes()
        with self.assertRaises(Denied):
            fresh.append(Charge("three", "alpha", 2))
        with self.assertRaisesRegex(Denied, "Duplicate"):
            fresh.append(Charge("three", "alpha", 3))
        self.assertEqual(fresh.log_path.read_bytes(), body)
        self.assertEqual(fresh.counts()["shared"], 3)
        self.assertEqual(len(fresh.read_from(0)), 3)

        with fresh.log_path.open("ab") as stream:
            stream.write(b"damaged line\n")
        with self.assertRaises(Denied):
            fresh.recover()

    def test_failed_counts_write_is_derived_on_next_recovery(self):
        self.ledger.append(Charge("one", "alpha", 0))
        original = self.ledger._write_counts

        def fail_once(counts):
            self.ledger._write_counts = original
            raise OSError("simulated sidecar failure")

        self.ledger._write_counts = fail_once
        with self.assertRaises(OSError):
            self.ledger.append(Charge("two", "beta", 1))
        self.assertEqual(self.ledger.counts(), {"shared": 1, "projects": {"alpha": 1}})

        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            self.assertEqual(fresh.recover(), 1)
            self.assertTrue(fresh.has("beta", "two"))
            fresh.append(Charge("three", "alpha", 2))
        self.assertEqual(fresh.counts()["shared"], 3)
        self.assertEqual([charge for charge, _ in fresh.read_from(0)],
                         [Charge("one", "alpha", 0), Charge("two", "beta", 1),
                          Charge("three", "alpha", 2)])

    def test_durable_log_append_precedes_sidecar_updates(self):
        calls = []
        real_fsync = os.fsync
        original_log = self.ledger._append_log
        original_counts = self.ledger._write_counts
        original_key = self.ledger._add_key

        def log(line):
            calls.append("log")
            original_log(line)

        def counts(value):
            calls.append("counts")
            original_counts(value)

        def key(value, **kwargs):
            calls.append("key")
            original_key(value, **kwargs)

        def fsync(fd):
            calls.append("fsync")
            return real_fsync(fd)

        self.ledger._append_log = log
        self.ledger._write_counts = counts
        self.ledger._add_key = key
        with mock.patch.object(os, "fsync", fsync):
            with self.ledger.lock():
                self.ledger.append(Charge("req-1", "alpha", 0))
        self.assertEqual(calls.count("log"), 1)
        self.assertEqual(calls.count("key"), 1)
        self.assertEqual(calls.count("counts"), 1)
        self.assertLess(calls.index("log"), calls.index("fsync"))
        self.assertLess(calls.index("fsync"), calls.index("key"))
        self.assertLess(calls.index("key"), calls.index("counts"))

    def test_day_validation_and_caller_lock(self):
        for day in ("2026-1-1", "20260101", "not-a-day", 123, "2026-02-30"):
            with self.subTest(day=day):
                with self.assertRaises(Denied):
                    DayLedger(self.root, day)
        with self.ledger.lock():
            self.ledger.append(Charge("under-lock", "alpha", 0))
        self.assertTrue(self.ledger.lock_path.exists())
        self.assertEqual(self.ledger.counts(), {"shared": 1, "projects": {"alpha": 1}})


if __name__ == "__main__":
    unittest.main()
