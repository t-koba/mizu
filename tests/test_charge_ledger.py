"""Append-only charge ledger: bounded records, constant-cost writes, sidecar reads."""
import json
import math
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401 -- puts src on sys.path for the standalone unittest run

from mizu.charge_ledger import (
    MAX_COUNTS_BYTES, MAX_IDS, MAX_LINE_BYTES, MAX_OFFSET_BYTES, MAX_PROJECTS,
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

    def _offset(self, ledger=None):
        return json.loads((ledger or self.ledger).offset_path.read_bytes())

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



    def test_fresh_recovery_seeks_past_a_damaged_log_head(self):
        total = 5
        with self.ledger.lock():
            for i in range(total):
                self.ledger.append(Charge(f"id-{i}", "alpha", i))
        line_length = len(charge_line(Charge("id-0", "alpha", 0)))
        with self.ledger.log_path.open("r+b") as stream:
            stream.write(b"damaged head\n" + b"x" * (line_length - len(b"damaged head\n")))
        # A scan from byte zero would fail closed on the damaged first line.
        with self.assertRaises(Denied):
            self.ledger.read_from(0)

        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            self.assertEqual(fresh.recover(), 0)
            self.assertEqual(fresh.recover(), 0)
        self.assertEqual(fresh.counts(), {"shared": total, "projects": {"alpha": total}})
        for i in range(total):
            self.assertTrue(fresh.has("alpha", f"id-{i}"))
        self.assertEqual(self._offset(fresh), {"shared": total, "offset": total * line_length})

    def test_fresh_recovery_replays_a_durable_tail_once(self):
        with self.ledger.lock():
            self.ledger.append(Charge("first", "alpha", 0))
        line = charge_line(Charge("tail", "beta", 1))
        checkpoint = self.ledger.log_path.stat().st_size
        with self.ledger.log_path.open("ab") as stream:
            stream.write(line)
        self.assertEqual(self.ledger.counts(), {"shared": 1, "projects": {"alpha": 1}})
        self.assertEqual(self._offset(), {"shared": 1, "offset": checkpoint})

        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            self.assertEqual(fresh.recover(), 1)
            self.assertEqual(fresh.recover(), 0)
        self.assertEqual(fresh.counts(), {"shared": 2, "projects": {"alpha": 1, "beta": 1}})
        self.assertTrue(fresh.has("alpha", "first"))
        self.assertTrue(fresh.has("beta", "tail"))
        self.assertEqual(self._offset(fresh), {"shared": 2, "offset": checkpoint + len(line)})
        self.assertEqual(fresh.read_from(2, start_offset=checkpoint + len(line)), ())

    def test_incoherent_checkpoints_fall_back_without_double_charging(self):
        with self.ledger.lock():
            self.ledger.append(Charge("one", "alpha", 0))
        first_size = self.ledger.log_path.stat().st_size
        tail = charge_line(Charge("two", "beta", 1))
        with self.ledger.log_path.open("ab") as stream:
            stream.write(tail)
        final_size = first_size + len(tail)
        key = self.ledger._key("beta", "two")
        bodies = (
            canonical({"shared": 0, "offset": first_size}),
            canonical({"shared": 1, "offset": final_size + 1}),
            canonical({"shared": 1, "offset": -1}),
            canonical({"shared": True, "offset": first_size}),
            b"{damaged\n",
            b"x" * (MAX_OFFSET_BYTES + 1),
        )
        for body in bodies:
            with self.subTest(body=body[:30]):
                self.ledger.counts_path.write_bytes(
                    canonical({"shared": 1, "projects": {"alpha": 1}}))
                (self.ledger.index_dir / key).unlink(missing_ok=True)
                self.ledger.offset_path.write_bytes(body)
                fresh = DayLedger(self.root, self.DAY)
                with fresh.lock():
                    self.assertEqual(fresh.recover(), 1)
                    self.assertEqual(fresh.recover(), 0)
                self.assertEqual(fresh.counts(), {"shared": 2,
                                                  "projects": {"alpha": 1, "beta": 1}})
                self.assertTrue(fresh.has("alpha", "one"))
                self.assertTrue(fresh.has("beta", "two"))
                self.assertEqual(self._offset(fresh), {"shared": 2, "offset": final_size})

    def test_trusted_checkpoint_on_the_wrong_line_fails_closed(self):
        with self.ledger.lock():
            self.ledger.append(Charge("one", "alpha", 0))
            self.ledger.append(Charge("two", "beta", 1))
        head = len(charge_line(Charge("one", "alpha", 0)))
        self.ledger.offset_path.write_bytes(canonical({"shared": 2, "offset": head}))
        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            with self.assertRaisesRegex(Denied, "sequence is inconsistent"):
                fresh.recover()
        self.assertEqual(fresh.counts(), {"shared": 2,
                                          "projects": {"alpha": 1, "beta": 1}})
        self.assertEqual(self._offset(fresh), {"shared": 2, "offset": head})

    @staticmethod
    def _trace_sidecars(ledger):
        calls = []
        original_counts = ledger._write_counts
        original_offset = ledger._write_offset

        def counts(value):
            calls.append("counts")
            original_counts(value)

        def offset(shared, position):
            calls.append("offset")
            original_offset(shared, position)

        ledger._write_counts = counts
        ledger._write_offset = offset
        return calls

    def test_failed_offset_write_is_repaired_without_double_charging(self):
        with self.ledger.lock():
            self.ledger.append(Charge("one", "alpha", 0))
        original = self.ledger._write_offset

        def fail(shared, position):
            self.ledger._write_offset = original
            raise OSError("simulated offset sidecar failure")

        self.ledger._write_offset = fail
        with self.assertRaises(OSError):
            self.ledger.append(Charge("two", "beta", 1))
        self.assertEqual(self.ledger.counts(), {"shared": 2, "projects": {"alpha": 1, "beta": 1}})
        self.assertEqual(self._offset(), {"shared": 1, "offset": len(charge_line(Charge("one", "alpha", 0)))})

        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            self.assertEqual(fresh.recover(), 0)
            self.assertEqual(fresh.recover(), 0)
        self.assertEqual(fresh.counts(), {"shared": 2, "projects": {"alpha": 1, "beta": 1}})
        self.assertTrue(fresh.has("beta", "two"))
        self.assertEqual(self._offset(fresh),
                         {"shared": 2, "offset": fresh.log_path.stat().st_size})

    def test_offset_is_published_after_counts_in_append_and_recovery(self):
        calls = self._trace_sidecars(self.ledger)
        with self.ledger.lock():
            self.ledger.append(Charge("one", "alpha", 0))
        self.assertEqual(calls, ["counts", "offset"])

        line = charge_line(Charge("two", "beta", 1))
        with self.ledger.log_path.open("ab") as stream:
            stream.write(line)
        fresh = DayLedger(self.root, self.DAY)
        calls = self._trace_sidecars(fresh)
        with fresh.lock():
            self.assertEqual(fresh.recover(), 1)
        self.assertEqual(calls, ["counts", "offset"])

    def test_recovery_advances_checkpoint_across_pages(self):
        total = READ_PAGE + 5
        with self.ledger.lock():
            for i in range(total):
                self.ledger.append(Charge(f"id-{i}", "alpha", i))
        size = self.ledger.log_path.stat().st_size
        # Rewind the counts checkpoint with a matching zero offset: a cold
        # recovery must replay every record in bounded pages.
        self.ledger.counts_path.write_bytes(canonical({"shared": 0, "projects": {}}))
        self.ledger.offset_path.write_bytes(canonical({"shared": 0, "offset": 0}))
        fresh = DayLedger(self.root, self.DAY)
        calls = self._trace_sidecars(fresh)
        with fresh.lock():
            self.assertEqual(fresh.recover(), total)
        self.assertEqual(calls, ["counts", "offset"] * 2)
        self.assertEqual(fresh.counts(), {"shared": total, "projects": {"alpha": total}})
        self.assertEqual(self._offset(fresh), {"shared": total, "offset": size})

    def test_read_from_start_offset_is_bounded_and_checks_ordinal(self):
        with self.ledger.lock():
            self.ledger.append(Charge("one", "alpha", 0))
            self.ledger.append(Charge("two", "beta", 1))
        size = self.ledger.log_path.stat().st_size
        head = size - len(charge_line(Charge("two", "beta", 1)))
        self.assertEqual(self.ledger.read_from(1, start_offset=head),
                         ((Charge("two", "beta", 1), 2),))
        self.assertEqual(self.ledger.read_from(2, start_offset=size), ())
        for bad in (-1, size + 1, True):
            with self.subTest(bad=bad):
                with self.assertRaises(Denied):
                    self.ledger.read_from(0, start_offset=bad)
        with self.assertRaisesRegex(Denied, "sequence is inconsistent"):
            self.ledger.read_from(2, start_offset=head)
        # Public reads still scan from byte zero.
        self.assertEqual(len(self.ledger.read_from(0)), 2)

    def test_admit_writes_one_bounded_line_per_charge(self):
        total = 2000

        def identity(i):
            # Compensate for the ordinal's digit count as _even() does.
            digits = len(str(i))
            return "r" * (12 - 2 * digits) + str(i)

        expected = len(charge_line(Charge(identity(0), "alpha", 0)))
        deltas = []
        with self.ledger.lock():
            for i in range(total):
                before = self.ledger.log_path.stat().st_size if self.ledger.log_path.exists() else 0
                self.assertEqual(self.ledger.admit("alpha", identity(i), daily=total), i + 1)
                deltas.append(self.ledger.log_path.stat().st_size - before)
        self.assertEqual(min(deltas), expected)
        self.assertEqual(max(deltas), expected)
        self.assertLessEqual(max(deltas), MAX_LINE_BYTES)
        self.assertEqual(self.ledger.log_path.stat().st_size, total * expected)
        self.assertEqual(self.ledger.counts(), {"shared": total, "projects": {"alpha": total}})
        for i in range(total):
            self.assertTrue(self.ledger.has("alpha", identity(i)))

    def test_admit_repeat_returns_the_scope_count_without_a_new_charge(self):
        with self.ledger.lock():
            self.assertEqual(self.ledger.admit("alpha", "same", daily=5), 1)
            self.assertEqual(self.ledger.admit("beta", "other", daily=5), 1)
            self.assertEqual(self.ledger.admit("", "shared-id", daily=5), 3)
            body = self.ledger.log_path.read_bytes()
            counts = self.ledger.counts()
            self.assertEqual(self.ledger.admit("alpha", "same", daily=1), 1)
            self.assertEqual(self.ledger.admit("beta", "other", daily=5), 1)
            self.assertEqual(self.ledger.admit("", "shared-id", daily=5), 3)
        self.assertEqual(self.ledger.log_path.read_bytes(), body)
        self.assertEqual(self.ledger.counts(), counts)
        # A scope owns its key space: the same id elsewhere is a new charge.
        with self.ledger.lock():
            self.assertEqual(self.ledger.admit("gamma", "same", daily=5), 1)
        self.assertEqual(self.ledger.counts()["shared"], 4)

    def test_admit_project_and_shared_exhaustion_fail_closed(self):
        with self.ledger.lock():
            self.assertEqual(self.ledger.admit("alpha", "a1", daily=2), 1)
            self.assertEqual(self.ledger.admit("alpha", "a2", daily=2), 2)
            for i in range(3):
                self.assertEqual(self.ledger.admit("beta", f"b{i}", daily=3), i + 1)
            # Another project's count neither exhausts nor frees alpha's quota.
            with self.assertRaises(InfraExceeded) as caught:
                self.ledger.admit("alpha", "a3", daily=2)
            self.assertEqual(str(caught.exception),
                             "daily model-request budget exhausted for project alpha")
            self.assertEqual(self.ledger.admit("alpha", "a3", daily=3), 3)
            body = self.ledger.log_path.read_bytes()
            counts = self.ledger.counts()
            with self.assertRaises(InfraExceeded) as caught:
                self.ledger.admit("alpha", "a4", daily=3)
            self.assertEqual(str(caught.exception),
                             "daily model-request budget exhausted for project alpha")
            with self.assertRaises(InfraExceeded) as caught:
                self.ledger.admit("", "s1", daily=6)
            self.assertEqual(str(caught.exception), "daily model-request budget exhausted")
            with self.assertRaises(InfraExceeded) as caught:
                self.ledger.admit("gamma", "g1", daily=5, shared_daily=6)
            self.assertEqual(str(caught.exception),
                             "daily model-request shared budget exhausted")
            with self.assertRaises(InfraExceeded) as caught:
                self.ledger.admit("", "s2", daily=100, shared_daily=6)
            self.assertEqual(str(caught.exception),
                             "daily model-request shared budget exhausted")
        self.assertEqual(self.ledger.log_path.read_bytes(), body)
        self.assertEqual(self.ledger.counts(), counts)

    def test_concurrent_admits_are_exact_and_idempotent(self):
        projects = ("alpha", "beta", "gamma")
        per_project = 12
        unique = [(project, i) for project in projects for i in range(per_project)]

        def charge(task):
            project, index = task
            with self.ledger.lock():
                return self.ledger.admit(project, f"{project}-{index}", daily=per_project)

        # Concurrent first admits take each project's sequence in some order;
        # concurrent repeats then see the settled count and charge nothing.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(charge, unique))
            repeats = list(pool.map(charge, unique))
        for project in projects:
            own = sorted(value for (name, _), value in zip(unique, results)
                         if name == project)
            self.assertEqual(own, list(range(1, per_project + 1)))
        self.assertEqual(repeats, [per_project] * len(unique))
        expected_counts = {"shared": len(unique),
                           "projects": {project: per_project for project in projects}}
        self.assertEqual(self.ledger.counts(), expected_counts)
        charges = [charge for charge, _ in self.ledger.read_from(0)]
        self.assertEqual(len(charges), len(unique))
        self.assertEqual([charge.seq for charge in charges], list(range(len(unique))))
        self.assertEqual({(charge.project, charge.id) for charge in charges},
                         {(project, f"{project}-{i}") for project, i in unique})
        for project, i in unique:
            self.assertTrue(self.ledger.has(project, f"{project}-{i}"))

    def test_admit_recovers_a_durable_tail_and_a_failed_sidecar_once(self):
        with self.ledger.lock():
            self.assertEqual(self.ledger.admit("alpha", "one", daily=10), 1)
        checkpoint = self.ledger.log_path.stat().st_size
        tail = charge_line(Charge("two", "beta", 1))
        with self.ledger.log_path.open("ab") as stream:
            stream.write(tail)
        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            self.assertEqual(fresh.admit("beta", "two", daily=10), 1)
            self.assertEqual(fresh.admit("alpha", "one", daily=10), 1)
            self.assertEqual(fresh.admit("gamma", "three", daily=10), 1)
        final_size = checkpoint + len(tail) + len(charge_line(Charge("three", "gamma", 2)))
        self.assertEqual(fresh.log_path.stat().st_size, final_size)
        self.assertEqual(fresh.counts(), {"shared": 3, "projects": {
            "alpha": 1, "beta": 1, "gamma": 1}})

        original = fresh._write_counts

        def fail_once(counts):
            fresh._write_counts = original
            raise OSError("simulated sidecar failure")

        fresh._write_counts = fail_once
        with fresh.lock():
            with self.assertRaises(OSError):
                fresh.admit("alpha", "four", daily=10)
        self.assertEqual(fresh.counts()["shared"], 3)
        durable = fresh.log_path.read_bytes()
        restart = DayLedger(self.root, self.DAY)
        with restart.lock():
            self.assertEqual(restart.admit("alpha", "four", daily=10), 2)
            self.assertEqual(restart.admit("beta", "two", daily=10), 1)
        self.assertEqual(restart.log_path.read_bytes(), durable)
        self.assertEqual(restart.counts()["shared"], 4)
        self.assertEqual(len(restart.read_from(0)), 4)

    def test_admit_refuses_malformed_counts_and_replays_a_lost_sidecar(self):
        with self.ledger.lock():
            self.assertEqual(self.ledger.admit("alpha", "one", daily=10), 1)
        body = self.ledger.log_path.read_bytes()
        self.ledger.counts_path.write_bytes(canonical({"shared": -1, "projects": {}}))
        with self.ledger.lock():
            with self.assertRaises(Denied):
                self.ledger.admit("alpha", "two", daily=10)
        self.assertEqual(self.ledger.log_path.read_bytes(), body)

        self.ledger.counts_path.unlink()
        fresh = DayLedger(self.root, self.DAY)
        with fresh.lock():
            self.assertEqual(fresh.admit("alpha", "two", daily=10), 2)
            self.assertEqual(fresh.admit("alpha", "one", daily=10), 2)
        self.assertEqual(fresh.counts(), {"shared": 2, "projects": {"alpha": 2}})
        self.assertEqual(len(fresh.read_from(0)), 2)

    def test_admit_invalid_limits_fail_closed(self):
        with self.ledger.lock():
            self.assertEqual(self.ledger.admit("alpha", "one", daily=10), 1)
        body = self.ledger.log_path.read_bytes()
        counts = self.ledger.counts()
        for name, value in ((name, value) for name in ("daily", "shared_daily")
                            for value in (-1, True, 1.5, math.nan, "10", None)):
            with self.subTest(name=name, value=value):
                with self.ledger.lock():
                    with self.assertRaises(Denied):
                        if name == "daily":
                            self.ledger.admit("alpha", "two", daily=value)
                        else:
                            self.ledger.admit("alpha", "two", daily=10, shared_daily=value)
                self.assertEqual(self.ledger.log_path.read_bytes(), body)
                self.assertEqual(self.ledger.counts(), counts)


if __name__ == "__main__":
    unittest.main()
