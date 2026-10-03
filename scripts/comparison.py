#!/usr/bin/env python3
"""Prepare a fixed local task and validate external comparison evidence.

No engine is invoked, no model or price is selected, and no result is invented.
An operator runs Mizu/OpenHands/Aider against separate identical task copies.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCENARIOS=('normal','recovery','idle','authority','operation')
METRICS=('provider_requests','invocations','input_tokens','output_tokens',
         'cost','elapsed_seconds','resume_seconds','storage_bytes','interventions','idle_invocations')


def fixture_manifest():
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted((ROOT/'examples/comparison').iterdir()) if p.is_file()}
    payload=json.dumps(files,sort_keys=True,separators=(',',':')).encode()
    return {'files':files,'sha256':hashlib.sha256(payload).hexdigest()}


def prepare(destination):
    manifest=fixture_manifest()
    destination.mkdir(parents=True,exist_ok=False)
    for name in manifest['files']:
        (destination/name).write_bytes((ROOT/'examples/comparison'/name).read_bytes())
    (destination/'fixture.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def validate(record):
    """Schema 1; <=64KiB JSON, finite nonnegative metrics or explicit null.

    Trust: operator evidence, not independently proved. No retries/engine calls.
    Failure: reject incomplete, unbound or malformed records before export.
    """
    if not isinstance(record,dict) or len(json.dumps(record,allow_nan=False).encode())>65536:
        raise ValueError('Evidence must be a JSON object of at most 64KiB')
    required={'product','version','scenario','trial','fixture_sha256','condition',
              'status','started_at','finished_at','evidence','metrics','limitations'}
    if set(record)!=required:raise ValueError('Invalid evidence structure')
    if record['product'] not in ('mizu','openhands','aider'):raise ValueError('Unknown product')
    if record['scenario'] not in SCENARIOS:raise ValueError('Unknown scenario')
    if record['status'] not in ('pass','fail','not_run','not_applicable'):raise ValueError('Invalid status')
    if type(record['trial']) is not int or not 1<=record['trial']<=1000:raise ValueError('Invalid trial')
    if record['fixture_sha256']!=fixture_manifest()['sha256']:raise ValueError('Fixture mismatch')
    condition=record['condition']
    if not isinstance(condition,dict) or set(condition)!={'provider','model','request_limit','permissions'}:
        raise ValueError('Explicit comparison conditions required')
    for key in ('provider','model','permissions'):
        if not isinstance(condition[key],str) or not 1<=len(condition[key])<=1000:raise ValueError('Invalid condition')
    if type(condition['request_limit']) is not int or not 0<=condition['request_limit']<=2**63-1:
        raise ValueError('Invalid request limit')
    for key in ('version','started_at','finished_at'):
        if not isinstance(record[key],str) or len(record[key])>200:raise ValueError('Invalid metadata')
    for key in ('evidence','limitations'):
        if not isinstance(record[key],list) or len(record[key])>100 or any(not isinstance(v,str) or len(v)>2000 for v in record[key]):
            raise ValueError('Invalid evidence references')
    metrics=record['metrics']
    if not isinstance(metrics,dict) or set(metrics)!=set(METRICS):raise ValueError('Missing independent metrics')
    for value in metrics.values():
        if value is not None and (type(value) not in (float,int) or not 0<=value<=2**63-1 or (type(value) is float and not math.isfinite(value))):
            raise ValueError('Metric must be finite nonnegative or null')
    if record['status'] in ('pass','fail'):
        if not record['evidence'] or not record['version']:
            raise ValueError('Executed trials require version and evidence')
        start,end=(dt.datetime.fromisoformat(record[key]) for key in ('started_at','finished_at'))
        if start.tzinfo is None or end.tzinfo is None or end<start:
            raise ValueError('Aware start/end times required in chronological order')
    elif any(value is not None for value in metrics.values()) or not record['limitations']:
        raise ValueError('Unexecuted/inapplicable trials need null metrics and a reason')
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='action',required=True)
    sub=subs.add_parser('prepare');sub.add_argument('destination',type=Path)
    sub=subs.add_parser('validate');sub.add_argument('record',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='prepare':result=prepare(args.destination)
        else:
            if args.record.stat().st_size>65536:raise ValueError('Evidence too large')
            result=validate(json.loads(args.record.read_bytes()))
    except (ValueError,OSError,TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
