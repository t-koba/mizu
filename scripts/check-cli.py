#!/usr/bin/env python3
"""Installed adapter contracts without model inference or credential access."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from mizu.doctor import help_has_flag
from mizu.engine_config import adapter_contract
from mizu.errors import ConfigError


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
    dependency = ROOT/'adapters/pi/node_modules' if engine in ('pi', 'pi-durable') else None
    return [*prefix, launcher, '--check-contract'], dependency, ('launcher_exports', list(contract['exports']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--claude-python',help='Interpreter of the isolated SDK environment')
    parser.add_argument('--codex',default='codex',help='Operator-selected app-server executable')
    args=parser.parse_args()
    prefixes={'pi':['node'],'codex':[args.codex],'claude':[args.claude_python or sys.executable]}
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
        result=subprocess.run(command,capture_output=True,text=True,timeout=30)
        if engine=='codex':
            missing=[flag for flag in expected if not help_has_flag(result.stdout,flag)]
            success=result.returncode==0 and not missing
            evidence={'required_flags':expected,'missing':missing}
        else:
            try:
                reported=json.loads(result.stdout.strip())
                verified=reported.get('exports',[]) if isinstance(reported,dict) else []
            except (json.JSONDecodeError,UnicodeError):
                verified=[]
            missing=[name for name in expected if name not in verified]
            if engine=='claude' and result.returncode and 'No module named' in result.stdout+result.stderr:
                checks.append({'engine':engine,'status':'not_run','reason':'Isolated SDK interpreter not configured'})
                continue
            success=result.returncode==0 and not missing
            evidence={'exports':sorted(set(verified)),'missing':missing}
        checks.append({'engine':engine,'status':'pass' if success else 'fail','exit_code':result.returncode,
                       'request_unit':adapter_contract(engine)['request_unit'],'contract':evidence})
    receipt={'kind':'installed-adapter-capability-contract','checks':checks,'inference':'not_run',
             'mcp_connection':'not_run','tool_isolation':'not_run','latest_distribution':'See upstream evidence separately'}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))
    return int(any(item['status']=='fail' for item in checks))

if __name__=='__main__':raise SystemExit(main())
