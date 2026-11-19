#!/usr/bin/env python3
"""Compatibility shim for the stage-2 decision adapter.

The adapter now lives in :mod:`mizu.queue_decision_delivery` as the production
non-shared stage-2 intermediate. This path stays importable so stage-2
comparison tests and operator scripts run unchanged. Patch and import it as
the production module: both names refer to the same module object.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mizu.queue_decision_delivery import *  # noqa: E402,F401,F403
from mizu import queue_decision_delivery as _implementation  # noqa: E402

sys.modules[__name__] = _implementation
