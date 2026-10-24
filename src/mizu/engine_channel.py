"""Bounded duplex JSONL driver channel; no retries after partial input delivery.

Transport parsing/memory safety is fixed mechanism: 64 KiB pipe reads, a
128-deep parsed-record queue, and a fixed per-record byte bound. Aggregate
event-stream retention is operator policy (`[limits] event_stream_bytes`,
defaulting to the mechanism default): bytes past the budget are dropped
from the retained ``*-events.jsonl`` file with an explicit
``*-events-truncated.json`` marker while parsing and the run continue, so
diagnostic volume can never discard already-sealed publishable work. The
gap is always recorded, never silent.
"""
import contextlib
import queue
import subprocess
import threading
import time

from . import platform
from .drivers import EVENT_RECORD_BYTES, EVENT_STREAM_BYTES, DIAGNOSTICS_TAIL_BYTES, parse_event
from .errors import Cancelled, LimitExceeded, ProtocolError
from .fs import atomic_write, canonical, now
from .process import send_bounded, terminate


def stream_budget(context) -> int:
    """Aggregate event-stream retention budget for one channel invocation.

    Schema: run context carrying ``config.limits.event_stream_bytes``.
    Bounds: the operator value within its configured range; any missing or
    invalid value falls back to the mechanism default (config load already
    fail-closes invalid files). Trust: operator policy, never model input.
    Failure: never raises; the fallback keeps the channel bounded.
    """
    try:
        value = int(context.config.limits.event_stream_bytes)
    except (AttributeError, TypeError, ValueError):
        return EVENT_STREAM_BYTES
    return value if value > 0 else EVENT_STREAM_BYTES


class Channel:
    def __init__(self, context, argv, env, cwd, label):
        self.context, self.label = context, label
        self.process = platform.spawn(argv, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **platform.popen_kwargs())
        self.records = queue.Queue(maxsize=128)
        self.stop = threading.Event()
        self.stream_budget = stream_budget(context)
        self.raw, self.diagnostics = bytearray(), bytearray()
        self.retained_bytes, self.dropped_bytes = 0, 0
        self.truncated = False
        self.readers = []
        for stream, events in ((self.process.stdout, True), (self.process.stderr, False)):
            platform.prepare_pipe(stream)
            thread = threading.Thread(target=self._read, args=(stream, events), daemon=True)
            self.readers.append(thread)
            thread.start()

    def _put(self, item):
        while not self.stop.is_set():
            try:
                self.records.put(item, timeout=.1)
                return
            except queue.Full:
                pass

    def _read(self, stream, events):
        pending = bytearray()
        try:
            while not self.stop.is_set():
                if not platform.wait_pipe(stream, self.stop):
                    continue
                try:
                    block = platform.read_pipe(stream, 65536)
                except BlockingIOError:
                    self.stop.wait(.01)
                    continue
                if not block:
                    if events and pending:
                        raise ProtocolError('Incomplete engine JSONL record')
                    break
                if not events:
                    self.diagnostics.extend(block)
                    if len(self.diagnostics) > DIAGNOSTICS_TAIL_BYTES:
                        del self.diagnostics[:len(self.diagnostics)-DIAGNOSTICS_TAIL_BYTES]
                    continue
                # Retention is capped, parsing is not: every record is still
                # dispatched, so the run completes on the same events regardless
                # of the operator's retention budget. The gap stays explicit in
                # `truncated` and the sidecar written by close().
                if self.retained_bytes < self.stream_budget:
                    keep = block[:max(0, self.stream_budget-self.retained_bytes)]
                    self.raw.extend(keep)
                    self.retained_bytes += len(keep)
                    if len(keep) < len(block):
                        self.truncated = True
                        self.dropped_bytes += len(block)-len(keep)
                else:
                    self.truncated = True
                    self.dropped_bytes += len(block)
                pending.extend(block)
                while b'\n' in pending:
                    end = pending.index(b'\n')+1
                    if end > EVENT_RECORD_BYTES:
                        raise ProtocolError('Engine record exceeds transport bound')
                    try:
                        event = parse_event(pending[:end])
                    except (ValueError, UnicodeError) as exc:
                        raise ProtocolError("Engine stdout is not valid finite JSONL") from exc
                    del pending[:end]
                    if not isinstance(event, dict):
                        raise ProtocolError('Engine record must be an object')
                    self._put(event)
                if len(pending) > EVENT_RECORD_BYTES:
                    raise ProtocolError('Engine record exceeds transport bound')
        except Exception as exc:
            if events:
                self._put(exc)
        finally:
            if events:
                self._put(None)

    def send(self, record):
        deadline = self.context.transport_deadline()
        try:
            send_bounded(self.process, canonical(record),
                         deadline=deadline if deadline is not None else time.monotonic() + self.context.config.limits.model_request_seconds,
                         cancel=self.context.transport_cancelled)
        except (Cancelled, LimitExceeded):
            # This send uses the context's timers/predicate. Classify only its
            # timeout/cancellation, never protocol or local persistence faults.
            self.context.check_deadlines(wrap_up=True)
            raise

    def receive(self, until=None, bound=None):
        while True:
            self.context.check_deadlines(wrap_up=True)
            if self.context.joins_idle.is_set() and until is not None and time.monotonic() >= until:
                # The bound that ended the wait is named: a child that was
                # stopped for time never said the engine was unusable.
                if bound:
                    raise ProtocolError('Engine handshake exceeded its %d s bound' % bound)
                raise ProtocolError('Engine handshake timed out')
            try:
                item = self.records.get(timeout=.1)
            except queue.Empty:
                continue
            if isinstance(item, Exception):
                raise item
            if item is None:
                self.context.wait_for_joins()
                self.context.check_deadlines()
                raise ProtocolError('Engine exited before terminal event')
            return item

    @property
    def quiescent(self):
        """Teardown proof, not a provider-completion or request-unit claim.

        Termination and reader joins are bounded; close returning alone does
        not prove they succeeded. A driver checkpoint must fail closed here.
        """
        return (self.stop.is_set() and self.process.poll() is not None
                and all(not thread.is_alive() for thread in self.readers))

    def close(self):
        self.context.wait_for_joins()
        self.stop.set()
        with contextlib.suppress(OSError, ValueError):
            self.process.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=1)
        terminate(self.process, grace=1)
        for thread in self.readers:
            thread.join(timeout=.5)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
        from .engine_config import stop_engine_containers
        try:
            atomic_write(self.context.run_dir / (self.label+'-events.jsonl'), memoryview(self.raw))
            atomic_write(self.context.run_dir / 'diagnostics.txt', bytes(self.diagnostics))
            if self.truncated:
                atomic_write(self.context.run_dir / (self.label+'-events-truncated.json'),
                             canonical({"label": self.label, "truncated": True,
                                        "stream_budget": self.stream_budget,
                                        "retained_bytes": self.retained_bytes,
                                        "dropped_bytes": self.dropped_bytes,
                                        "recorded_at": now(),
                                        "note": "Retained event evidence is capped at the operator budget; "
                                                "parsing and the run continued past the cap."}))
        finally:
            stop_engine_containers(self.context)
