#!/usr/bin/env python3
"""Installed adapter contracts without model inference or credential access."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from mizu.config import load
from mizu.doctor import (ADAPTER_STARTUP_FLOOR_SECONDS,
                         ADAPTER_STARTUP_MEASURE_CEILING_SECONDS,
                         probe_label,
                         probe_outcome,
                         startup_evidence,
                         help_has_flag)
from mizu.engine_config import adapter_contract
from mizu.errors import ConfigError
from mizu.platform import default_config_file
from mizu.startup import PROBE_ENDPOINT, observed_startup


def probe(engine, prefix):
    """Capability probe derived from adapters/<engine>/contract.json.

    Returns (argv, dependency_path_or_None, check_function_name).
    """
    contract = adapter_contract(engine)
    if engine == 'codex':
        argv = [*prefix, contract['command'][0], '--help']
        flags = list(contract['required_flags'])
        for item in contract.get('contracts', []):
            flags.extend(item['required_flags'])
        return argv, None, ('codex_flags', sorted(set(flags)))
    launcher = str(ROOT/'adapters'/engine/contract['entrypoint'])
    dependency = ROOT/'adapters'/engine/'node_modules' if engine in ('pi', 'pi-durable') else None
    return [*prefix, launcher, '--check-contract'], dependency, ('launcher_exports', list(contract['exports']))


class ProbeTimeout(ConfigError):
    """The adapter child outlived the host-relative probe bound."""


def bound_refusal(exc, bound, engine=None):
    """The shared classification of a child that did not answer.

    The doctor and this check share probe classification and engine labels.
    The reason, exit code and stderr are preserved without treating a stopped
    child as evidence of a missing capability.
    """
    detail = getattr(exc, "stderr", None)
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", "replace")
    label = probe_label(engine)
    try:
        probe_outcome("timeout", None, detail, label, bound)
    except ConfigError as refusal:
        raise ProbeTimeout(str(refusal)) from exc
    else:
        # The shared rule refuses a child that did not answer, so the
        # classification is the whole message; the local type is the only
        # part of the refusal that stays with the script.
        raise ProbeTimeout(f"{label} did not complete before it could report")


def host_config():
    """The operator's configuration, when this host's shared record is readable.

    The per-engine record is shared by the check and doctor. A configuration
    that cannot be read leaves the check with the bound its own measurement
    requires.
    """
    try:
        return load(default_config_file())
    except (OSError, ValueError, ConfigError, KeyError, TypeError):
        return None


def run_probe(command, engine=None, config=None):
    """Run one adapter child under this host's bound.

    Returns (result, cost_seconds, bound_seconds). The measuring run spans
    process spawn through exit and is capped at 150 s; any completed exit,
    including nonzero, contributes a sample. The per-engine record may also
    contain a runtime spawn-to-first-response interval. Those endpoints differ,
    and the larger recorded interval supplies the conservative bound. If the
    measurement does not complete, this check uses the 60 s floor. A child
    stopped before reporting is not evidence of a missing capability.
    """
    started = time.monotonic()
    try:
        measured = subprocess.run(command, capture_output=True, text=True,
                                  timeout=ADAPTER_STARTUP_MEASURE_CEILING_SECONDS)
    except subprocess.TimeoutExpired:
        measured = None
    startup = round(time.monotonic() - started, 3)
    if measured is not None and measured.returncode is not None and measured.returncode >= 0:
        # A completed command contributes its full spawn-to-exit interval;
        # the shared record retains the largest per-engine interval.
        if config is None and engine is not None:
            config = host_config()
        cost, bound, endpoint = observed_startup(config, engine, startup,
                                                 PROBE_ENDPOINT)
    else:
        # The measuring command did not complete within its ceiling. No
        # sample is recorded, so the probe uses the configured floor.
        bound = ADAPTER_STARTUP_FLOOR_SECONDS
        cost = None
        endpoint = None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=bound)
    except subprocess.TimeoutExpired as exc:
        bound_refusal(exc, bound, engine)
    return result, cost, bound, endpoint


def probe_engine(engine, command, expected, config=None):
    """Probe one installed adapter and return its check record."""
    try:
        result, cost, bound, endpoint = run_probe(command, engine=engine, config=config)
    except (ProbeTimeout, ConfigError) as exc:
        return {'engine': engine, 'status': 'fail', 'reason': str(exc)}
    if result.returncode is not None and result.returncode < 0:
        # The child was killed before it could report: it never said the
        # capability was missing, so it is not read as missing exports.
        try:
            # The child's own outcome goes to the shared classification, so
            # the script and the doctor name a killed child the same way.
            probe_outcome('exited', result.returncode, result.stderr,
                          probe_label(engine))
        except ConfigError as exc:
            return {'engine': engine, 'status': 'fail', 'reason': str(exc),
                    'exit_code': result.returncode}
    if engine == 'codex':
        missing = [flag for flag in expected if not help_has_flag(result.stdout, flag)]
        success = result.returncode == 0 and not missing
        evidence = {'required_flags': expected, 'missing': missing}
    else:
        try:
            reported = json.loads(result.stdout.strip())
            verified = reported.get('exports', []) if isinstance(reported, dict) else []
        except (json.JSONDecodeError, UnicodeError):
            verified = []
        missing = [name for name in expected if name not in verified]
        if engine == 'claude' and result.returncode and 'No module named' in result.stdout+result.stderr:
            return {'engine': engine, 'status': 'not_run',
                    'reason': 'Isolated SDK interpreter not configured'}
        success = result.returncode == 0 and not missing
        evidence = {'exports': sorted(set(verified)), 'missing': missing}
    # The cost published is the one the bound was derived from, and the
    # shape of that evidence is the doctor's, so one child reads the same
    # way wherever it is probed.
    evidence.update(startup_evidence(cost, bound, endpoint))
    return {'engine': engine, 'status': 'pass' if success else 'fail',
            'exit_code': result.returncode,
            'request_unit': adapter_contract(engine)['request_unit'], 'contract': evidence}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--claude-python',help='Interpreter of the isolated SDK environment')
    parser.add_argument('--codex',default='codex',help='Operator-selected app-server executable')
    args=parser.parse_args()
    prefixes={'pi':['node'],'pi-durable':['node'],'codex':[args.codex],'claude':[args.claude_python or sys.executable]}
    checks=[]
    for engine in ('pi','pi-durable','codex','claude'):
        try:
            command,dependency,(_,expected)=probe(engine,prefixes[engine])
        except ConfigError as exc:
            checks.append({'engine':engine,'status':'fail','reason':str(exc)})
            continue
        if not shutil.which(command[0]) or dependency is not None and not dependency.exists():
            checks.append({'engine':engine,'status':'not_run','reason':'Adapter environment not installed'})
            continue
        checks.append(probe_engine(engine, command, expected))
    receipt={'kind':'installed-adapter-capability-contract','checks':checks,'inference':'not_run',
             'mcp_connection':'not_run','tool_isolation':'not_run','latest_distribution':'See upstream evidence separately'}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))
    return int(any(item['status']=='fail' for item in checks))

if __name__=='__main__':raise SystemExit(main())
