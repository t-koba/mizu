"""Synthetic app-server/SDK process peers; no inference and no OCI claims."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from mizu.mcp_proxy import forward

engine = sys.argv[1]
scenario = sys.argv[2]

def emit(record):
    print(json.dumps(record), flush=True)


def finish(bridge):
    if scenario == 'missing-finish':
        return
    forward(bridge, 'finish', {'outcome': 'wait', 'summary': 'Synthetic result', 'state': 'Review evidence'})
    if scenario == 'post-finish':
        try:
            forward(bridge, 'read', {'path': 'app.py'})
        except Exception:
            return
        raise AssertionError('Post-seal operation unexpectedly allowed')

if scenario == 'bad-json':
    print('bad json', flush=True)
    raise SystemExit(0)
if scenario == 'exit':
    raise SystemExit(7)
if engine == 'claude':
    cfg = json.loads(Path(sys.argv[-1]).read_text())
    bridge = json.loads(Path(cfg['mcp_servers']['mizu']['env']['MIZU_BRIDGE_CONFIG']).read_text())
    forward(bridge, '_hello', {})
    emit({'type': 'ready'})
    query = json.loads(sys.stdin.readline())
    if scenario == 'query-error':
        emit({'type': 'error', 'stage': 'query', 'error_type': 'SyntheticProviderFailure', 'error': 'try elsewhere', 'code': 'synthetic-code', 'retry_at': 2000000000})
        sys.stdin.readline()
        raise SystemExit(0)
    finish(bridge)
    emit({'type': 'result', 'terminal_reason': 'api_error' if scenario == 'api-error' else 'completed',
          'subtype': 'success', 'is_error': False, 'session_id': cfg.get('resume') or 'session',
          'model_usage': {'actual-model': {'inputTokens': 10, 'outputTokens': 5}}})
    sys.stdin.readline()
else:
    bridge = None
    for line in sys.stdin:
        message = json.loads(line)
        method, identifier = message.get('method'), message.get('id')
        if method == 'initialize':
            emit({'id': identifier, 'result': {}})
        elif method in ('thread/start', 'thread/resume'):
            params = message['params']
            bridge = json.loads(Path(params['config']['mcp_servers']['mizu']['env']['MIZU_BRIDGE_CONFIG']).read_text())
            emit({'id': identifier, 'result': {'thread': {'id': params.get('threadId', 'thread')},
                  'model': params['model'], 'modelProvider': params['modelProvider']}})
        elif method == 'mcpServerStatus/list':
            tools = {tool['name']: {} for tool in bridge['tools']}
            emit({'id': identifier, 'result': {'data': [] if scenario == 'mcp-failure' else [{'name': 'mizu', 'tools': tools}]}})
        elif method == 'turn/start':
            if scenario == 'rpc-error':
                emit({'id': identifier, 'error': {'code': -32000, 'message': 'synthetic failure'}})
                continue
            if scenario == 'approval':
                emit({'id': 'ask', 'method': 'item/tool/requestUserInput', 'params': {}})
                continue
            turn = 'turn'
            finish(bridge)
            if scenario == 'native-after-finish':
                emit({'method': 'item/started', 'params': {'threadId': 'thread',
                      'item': {'type': 'webSearch', 'id': 'late-web'}}})
            # Terminal notification may precede the turn/start response.
            emit({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'turnId': turn,
                  'tokenUsage': {'total': {'inputTokens': 10, 'outputTokens': 5}}}})
            emit({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': turn,
                  'status': 'failed' if scenario in ('failed', 'provider-error') else 'interrupted' if scenario == 'interrupted' else 'completed',
                  **({'error': {'message': 'synthetic failure', 'code': 'synthetic-code', 'codexErrorInfo': {'synthetic': {'httpStatusCode': 503}}}} if scenario == 'provider-error' else {})}}})
            emit({'id': identifier, 'result': {'turn': {'id': turn}}})
        elif method == 'turn/interrupt':
            break
