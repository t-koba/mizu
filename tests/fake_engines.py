"""Synthetic app-server/SDK process peers; no inference and no OCI claims."""
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from mizu.mcp_proxy import forward

engine = sys.argv[1]
scenario = sys.argv[2]

def emit(record):
    print(json.dumps(record), flush=True)


def stall():
    # Progress is deliberately not a completion, admission, or usage event.
    while True:
        emit({'type': 'progress', 'method': 'synthetic/progress'})
        time.sleep(.01)


def finish(bridge, *, resumed=False):
    if scenario.startswith('recover-'):
        assert set(tool['operation'] for tool in bridge['tools']) <= {'files', 'read', 'diff', 'finish'}
        for operation, args in (('exec', {'script': 'false'}), ('research', {'state': {}}),
                                ('_engine_tool', {'name': 'x', 'invocation_id': 'x'})):
            try:
                forward(bridge, operation, args)
            except Exception:
                pass
            else:
                raise AssertionError('Recovery admitted '+operation)
        forward(bridge, 'read', {'path': 'app.py'})
        if scenario == 'recover-missing':
            return
        forward(bridge, 'finish', {'outcome': scenario.removeprefix('recover-'),
                                 'summary': 'Recovered existing workspace', 'state': 'Retained dirty work'})
        return
    if scenario.startswith('seal-'):
        if len(bridge['tools']) != 1:
            return  # Initial ordinary unit establishes the checkpoint/session.
        assert resumed, 'Finish-only phase forked the saved conversation'
        assert [tool['operation'] for tool in bridge['tools']] == ['finish']
        for operation, args in (('files', {}), ('_budget', {'sequence': 1}),
                                ('_engine_tool', {'name': 'x', 'invocation_id': 'x'}), ('_hello', {})):
            try:
                forward(bridge, operation, args)
            except Exception:
                pass
            else:
                raise AssertionError('Finish-only phase admitted '+operation)
        if scenario in ('seal-missing', 'seal-provider-error'):
            return
        if scenario == 'seal-failed':
            for _ in range(2):
                try:
                    forward(bridge, 'finish', {'outcome': 'wait', 'summary': 'cannot seal'})
                except Exception:
                    pass
                else:
                    raise AssertionError('Failed finish was retried successfully')
            return
        outcome = scenario.removeprefix('seal-')
        forward(bridge, 'finish', {'outcome': outcome, 'summary': 'Synthetic checkpoint seal', 'state': 'Retain state\nNext: Resume the synthetic task'})
        forward(bridge, '_model_usage', {'sequence': 1, 'usage': {'outputTokens': 5}})
        for operation, args in (('finish', {'outcome': 'continue', 'summary': 'must not overwrite'}),
                                ('files', {}), ('_budget', {'sequence': 2}), ('_hello', {}),
                                ('_request_done', {'sequence': 99})):
            try:
                forward(bridge, operation, args)
            except Exception:
                pass
            else:
                raise AssertionError('Post-finish operation admitted '+operation)
        return
    if scenario == 'checkpoint-tool':
        forward(bridge, 'files', {})
        return
    if scenario == 'checkpoint-join':
        forward(bridge, 'consult', {'question': 'synthetic child'})
        return
    if scenario in ('missing-finish', 'provider-error-before-finish'):
        return
    if scenario == 'finish-on-resume' and not resumed:
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
    if scenario == 'no-ready':
        # The child never answers its initial handshake: the caller's bound
        # is what ends the wait, and a silence is not an answer.
        while True:
            time.sleep(.05)
    if scenario == 'ready-error':
        emit({'type': 'error', 'stage': 'handshake', 'error': 'synthetic refusal'})
    else:
        emit({'type': 'ready'})
    query = json.loads(sys.stdin.readline())
    if scenario == 'stalled-request':
        stall()
    if scenario == 'query-error':
        emit({'type': 'error', 'stage': 'query', 'error_type': 'SyntheticProviderFailure', 'error': 'try elsewhere', 'code': 'synthetic-code', 'retry_at': 2000000000})
        sys.stdin.readline()
        raise SystemExit(0)
    finish(bridge, resumed=bool(cfg.get('resume')))
    api_error = scenario == 'api-error' or (scenario == 'seal-provider-error' and cfg.get('resume'))
    result = {'type': 'result', 'terminal_reason': 'api_error' if api_error else 'completed',
              'subtype': 'success', 'is_error': api_error, 'session_id': cfg.get('resume') or 'session',
              'model_usage': {'actual-model': {'inputTokens': 10, 'outputTokens': 5}}}
    if scenario == 'api-error':
        result['api_error_status'] = 429
    emit(result)
    sys.stdin.readline()
else:
    bridge = None
    resumed = False
    for line in sys.stdin:
        message = json.loads(line)
        method, identifier = message.get('method'), message.get('id')
        if method == 'initialize':
            if scenario == 'no-initialize':
                continue
            if scenario == 'initialize-error':
                emit({'id': identifier, 'error': {'code': -32000, 'message': 'synthetic refusal'}})
            else:
                emit({'id': identifier, 'result': {}})
        elif method in ('thread/start', 'thread/resume'):
            params = message['params']
            resumed = method == 'thread/resume'
            bridge = json.loads(Path(params['config']['mcp_servers']['mizu']['env']['MIZU_BRIDGE_CONFIG']).read_text())
            emit({'id': identifier, 'result': {'thread': {'id': params.get('threadId', 'thread')},
                  'model': params['model'], 'modelProvider': params['modelProvider']}})
        elif method == 'mcpServerStatus/list':
            tools = {tool['name']: {} for tool in bridge['tools']}
            emit({'id': identifier, 'result': {'data': [] if scenario == 'mcp-failure' else [{'name': 'mizu', 'tools': tools}]}})
        elif method == 'turn/start':
            if scenario == 'stalled-request':
                emit({'id': identifier, 'result': {'turn': {'id': 'turn'}}})
                stall()
            if scenario == 'rpc-error':
                emit({'id': identifier, 'error': {'code': -32000, 'message': 'synthetic failure'}})
                continue
            if scenario == 'approval':
                emit({'id': 'ask', 'method': 'item/tool/requestUserInput', 'params': {}})
                continue
            turn = 'turn'
            if scenario == 'other-thread-native':
                emit({'method': 'item/started', 'params': {'threadId': 'other-thread',
                      'item': {'type': 'webSearch', 'id': 'unrelated-web'}}})
            unexpected = {
                'native-before-finish': {'type': 'webSearch', 'id': 'unexpected-web'},
                'external-before-finish': {'type': 'mcpToolCall', 'server': 'fake', 'tool': 'search', 'id': 'unexpected-mcp'},
                'collab-before-finish': {'type': 'collabAgentToolCall', 'tool': 'spawnAgent', 'id': 'unexpected-child'},
            }.get(scenario)
            if unexpected:
                emit({'method': 'item/started', 'params': {'threadId': 'thread', 'item': unexpected}})
            else:
                finish(bridge, resumed=resumed)
            if scenario == 'native-after-finish':
                emit({'method': 'item/started', 'params': {'threadId': 'thread',
                      'item': {'type': 'webSearch', 'id': 'late-web'}}})
            # Terminal notification may precede the turn/start response.
            emit({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'turnId': turn,
                  'tokenUsage': {'total': {'inputTokens': 10, 'outputTokens': 5}}}})
            emit({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': turn,
                  'status': 'failed' if (scenario in ('failed', 'provider-error', 'provider-error-before-finish') or (scenario == 'seal-provider-error' and resumed)) else 'interrupted' if scenario == 'interrupted' else 'completed',
                  **({'error': {'message': 'synthetic failure', 'code': 'synthetic-code', 'codexErrorInfo': {'synthetic': {'httpStatusCode': 503}}}} if scenario in ('provider-error', 'provider-error-before-finish') else {})}}})
            emit({'id': identifier, 'result': {'turn': {'id': turn}}})
        elif method == 'turn/interrupt':
            break
