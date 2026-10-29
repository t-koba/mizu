import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs/promises';
import { request, typeSchema } from '../adapters/pi/bridge-client.mjs';
import { register } from '../adapters/pi/register.mjs';
import { meterRuntime } from '../adapters/pi/model-runtime.mjs';

// Loopback TCP works on every host; Unix-socket paths do not (Windows
// runners refuse them), so the shared helper binds TCP. Framing, auth,
// bounds and deadlines are transport-independent; one POSIX-only test below
// still covers the Unix-socket branch of request().
// Success-path tests synchronize on the bridge response, not on a short wall
// clock: the tight deadline is asserted only by the unresponsive-peer test.
async function server(t, respond, timeout = 5000) {
  const peers = new Set();
  const listener = net.createServer(peer => {
    peers.add(peer); peer.on('error', () => {}); peer.on('close', () => peers.delete(peer));
    let text = '';
    peer.on('data', chunk => { text += chunk; if (text.includes('\n')) respond(peer, JSON.parse(text.split('\n')[0])); });
  });
  await new Promise((resolve, reject) => { listener.once('error', reject); listener.listen(0, '127.0.0.1', resolve); });
  t.after(async () => {
    for (const peer of peers) peer.destroy();
    await new Promise(resolve => listener.close(resolve));
  });
  const { port } = listener.address();
  return { transport: 'tcp', host: '127.0.0.1', port, token: 'unit-test-token', timeout_ms: timeout };
}
const Type = Object.fromEntries(['Union', 'Literal', 'String', 'Integer', 'Boolean', 'Array', 'Object', 'Optional']
  .map(name => [name, (...args) => ({ name, args })]));

test('LF framing preserves Unicode separators and passes authenticated arguments', async t => {
  const cfg = await server(t, (peer, value) => {
    assert.equal(value.token, 'unit-test-token'); assert.equal(value.operation, 'read');
    assert.deepEqual(value.arguments, { path: 'a' });
    peer.end(JSON.stringify({ ok: true, result: {text:'a\u2028b\u2029c日本語'} }) + '\n');
  });
  assert.deepEqual(await request(cfg, 'read', { path: 'a' }), {text:'a\u2028b\u2029c日本語'});
});
test('server refusals remain failures', async t => {
  const cfg = await server(t, peer => peer.end('{"ok":false,"error":"Denied"}\n'));
  await assert.rejects(request(cfg, 'read', {}), /Denied/);
});
test('early close is not a successful empty result', async t => {
  const cfg = await server(t, peer => peer.end());
  await assert.rejects(request(cfg, 'read', {}), /closed/);
});
test('malformed responses fail closed', async t => {
  const cfg = await server(t, peer => peer.end('not-json\n'));
  await assert.rejects(request(cfg, 'read', {}), SyntaxError);
});
test('unresponsive peers hit a deadline', async t => {
  const cfg = await server(t, () => {}, 30);
  await assert.rejects(request(cfg, 'read', {}), /deadline/);
});
test('trickling peers cannot defer the deadline', async t => {
  const peers = new Set();
  const timers = new Set();
  const listener = net.createServer(peer => {
    peers.add(peer); peer.on('error', () => {}); peer.on('close', () => peers.delete(peer));
    const timer = setInterval(() => { try { peer.write('x'); } catch {} }, 20);
    timers.add(timer); peer.on('close', () => { clearInterval(timer); timers.delete(timer); });
  });
  await new Promise((resolve, reject) => { listener.once('error', reject); listener.listen(0, '127.0.0.1', resolve); });
  t.after(async () => {
    for (const timer of timers) clearInterval(timer);
    for (const peer of peers) peer.destroy();
    await new Promise(resolve => listener.close(resolve));
  });
  const { port } = listener.address();
  const start = Date.now();
  await assert.rejects(
    request({ transport: 'tcp', host: '127.0.0.1', port, token: 'unit-test-token', timeout_ms: 200 }, 'read', {}),
    /deadline/);
  assert.ok(Date.now() - start < 2000, 'deadline is absolute, not idle');
});
test('cancellation aborts a request', async t => {
  const cfg = await server(t, () => {});
  const controller = new AbortController();
  const response = request(cfg, 'read', {}, controller.signal);
  setTimeout(() => controller.abort(), 15);
  await assert.rejects(response, /Cancelled/);
});
test('pre-cancelled request does not connect', async () => {
  const controller = new AbortController(); controller.abort();
  await assert.rejects(request({ socket: '/nonexistent', timeout_ms: 10 }, 'read', {}, controller.signal), /Cancelled/);
});
test('request byte cap is enforced', async t => {
  const cfg = await server(t, () => {});
  await assert.rejects(request(cfg, 'read', { data: 'x'.repeat(1048576) }), /byte limit/);
});
test('response byte cap is enforced', async t => {
  const cfg = await server(t, peer => peer.end('x'.repeat(1048577)));
  await assert.rejects(request(cfg, 'read', {}), /byte limit/);
});
test('missing endpoint is refused without connecting', async () => {
  await assert.rejects(request({ timeout_ms: 10 }, 'read', {}), /endpoint/);
});
test('unix-socket endpoint still connects where the platform serves one', async t => {
  if (process.platform === 'win32') return t.skip('Unix transport unavailable on this platform'); // Covered by TCP above on Windows.
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'mizu-node-'));
  const socket = path.join(dir, 'bridge.sock');
  const listener = net.createServer(peer => {
    peer.on('error', () => {});
    let text = '';
    peer.on('data', chunk => {
      text += chunk;
      if (text.includes('\n')) peer.end(JSON.stringify({ ok: true, result: {text:'unix-ok'} }) + '\n');
    });
  });
  await new Promise((resolve, reject) => { listener.once('error', reject); listener.listen(socket, resolve); });
  t.after(async () => {
    await new Promise(resolve => listener.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  });
  assert.deepEqual(await request({ transport: "unix", socket, token: 'unit-test-token', timeout_ms: 5000 }, 'read', {}), {text:'unix-ok'});
});
test('schema conversion preserves required fields and denies extra properties', () => {
  const schema = typeSchema(Type, { type: 'object', properties: { x: { type: 'string', maxLength: 4 }, y: { type: 'boolean' } }, required: ['x'] });
  assert.equal(schema.args[0].x.name, 'String'); assert.equal(schema.args[0].y.name, 'Optional');
  assert.equal(schema.args[1].additionalProperties, false);
  assert.equal(typeSchema(Type, { enum: ['wait', 'done'] }).name, 'Union');
  assert.throws(() => typeSchema(Type, { type: 'unknown' }), /Unsupported/);
});
function registration(overrides = {}) {
  const tools = [], events = {}, active = [], calls = [];
  const pi = { registerTool: tool => tools.push(tool), on: (name, fn) => { events[name] = fn; }, setActiveTools: names => active.push(...names) };
  const cfg = { tools: [{ name: 'mizu_finish', operation: 'finish', description: 'End', inputSchema: { type: 'object', properties: {} } }] };
  register(pi, Type, cfg, { request: async (_cfg, operation, args) => { calls.push([operation, args]); return { recorded: true }; }, ...overrides });
  return { tools, events, active, calls };
}
test('extension authenticates startup and finish has structured model-only output', async () => {
  const r = registration(); await r.events.session_start();
  assert.equal(r.calls[0][0], '_hello');
  assert.equal(r.tools[0].exposure, 'model-only');
  assert.equal(r.tools[0].executionMode, 'sequential');
  const result = await r.tools[0].execute('id', {});
  assert.equal(result.terminate, true);
  assert.deepEqual(result.structuredContent, {recorded:true});
});
test('native and codemode nested calls use explicit bridge grants', async () => {
  const r = registration({ request: async (_cfg, op) => { if (op === '_engine_tool') throw Error('denied'); } });
  assert.equal((await r.events.tool_call({toolName:'ungranted',toolCallId:'parent/0',parentToolCallId:'parent'})).block,true);
});
test('Pi pre-call metadata identity is forwarded before synthetic server execution', async () => {
  const trace = [];
  const r = registration({ request: async (_cfg, op, args) => {
    assert.equal(op, '_engine_tool');
    assert.deepEqual(args, {name:'extra', invocation_id:'sdk-parent/0'});
    trace.push('admission');
    return {allowed:true};
  } });
  const result = await r.events.tool_call({type:'tool_call', toolName:'extra',
    toolCallId:'sdk-parent/0', parentToolCallId:'sdk-parent',
    input:{invocation_id:'model-forged-id', toolCallId:'model-forged-id'}});
  if (!result?.block) trace.push('fake-tool-server');
  assert.deepEqual(trace, ['admission', 'fake-tool-server']);
  await r.events.tool_call({toolName:'mizu_finish',toolCallId:'bridge-id'});
  assert.deepEqual(trace, ['admission', 'fake-tool-server']); // Bridge tools bypass native admission.
});

test('Pi pre-call denial or absent admission blocks before synthetic server execution', async () => {
  for (const mode of ['denied', 'missing-id', 'not-allowed', 'malformed-response']) {
    let reached = false, calls = 0;
    const r = registration({ request: async (_cfg, op, args) => {
      assert.equal(op, '_engine_tool');
      calls++;
      if (mode === 'denied') throw Error('denied by host');
      if (mode === 'missing-id') {
        assert.equal(args.invocation_id, undefined);
        throw Error('invalid invocation id');
      }
      return mode === 'not-allowed' ? {allowed:false} : {};
    } });
    const result = await r.events.tool_call({type:'tool_call', toolName:'extra',
      ...(mode === 'missing-id' ? {} : {toolCallId:'sdk-id'}),
      input:{invocation_id:'model-forged-id', toolCallId:'model-forged-id'}});
    if (!result?.block) reached = true;
    assert.equal(calls, 1);
    assert.equal(result.block, true);
    assert.equal(reached, false);
  }
});

function fakeRuntime() {
  return {
    stream(_model,_context,options) { const result = Promise.resolve({usage:{input:1}}); return {result:()=>result}; },
    streamSimple(model,context,options) { return this.stream(model,context,options); },
    complete(model,context,options) { return this.stream(model,context,options).result(); },
    completeSimple(model,context,options) { return this.streamSimple(model,context,options).result(); },
    async classify() { return {usage:{input:2}}; },
    async generateImages() { return {usage:{input:3}}; },
  };
}
test('runtime meters stream, complete, classifier and images without wrapper duplicates', async () => {
  const runtime=fakeRuntime(), calls=[];
  const meter=meterRuntime(runtime,{},async (_cfg,op,args)=>calls.push([op,args]));
  await Promise.all([runtime.completeSimple({},{}),runtime.complete({},{}),runtime.classify({},{}),runtime.generateImages({},{})]);
  await meter.flush();
  assert.equal(calls.filter(([op])=>op==='_budget').length,4);
  assert.equal(calls.filter(([op])=>op==='_model_usage').length,4);
  assert.equal(new Set(calls.filter(([op])=>op==='_budget').map(([,a])=>a.sequence)).size,4);
});
test('runtime refuses before provider dispatch', async () => {
  let dispatched=false;
  const runtime=fakeRuntime();
  runtime.classify=async()=>{dispatched=true;};
  meterRuntime(runtime,{},async()=>{throw Error('budget');});
  await assert.rejects(runtime.classify({},{}),/budget/);
  assert.equal(dispatched,false);
});

test('non-loopback and invalid-port endpoints are refused before connection', async () => {
  for (const endpoint of [{host:'192.0.2.1',port:80},{host:'127.0.0.1',port:true},{host:'127.0.0.1',port:65536}]) {
    await assert.rejects(request({transport:'tcp',...endpoint,token:'t',timeout_ms:10},'finish',{}), /endpoint/);
  }
});

test('bridge envelope requires boolean ok and object result', async t => {
  for (const response of [{ok:'true',result:{}},{ok:true,result:[]},{ok:true,result:null}]) {
    const config = await server(t, peer => peer.end(JSON.stringify(response)+'\n'));
    await assert.rejects(request(config,'read',{}), /Invalid bridge/);
  }
});

test('host-managed joins survive the caller RPC deadline without changing normal RPC bounds', async t => {
  const cfg = await server(t, socket => {
    setTimeout(() => socket.end('{"ok":true,"result":{"answer":"joined"}}\n'), 150);
  });
  cfg.timeout_ms = 50;
  cfg.joined_operations = ['delegate', 'consult'];
  assert.deepEqual(await request(cfg, 'delegate', {}), {answer:'joined'});
});

test('runtime completion pairs admission even without usage or on provider failure', async () => {
  const runtime=fakeRuntime(), calls=[];
  runtime.classify=async()=>({});
  runtime.generateImages=async()=>{throw Error('provider');};
  meterRuntime(runtime,{},async (_cfg,op,args)=>calls.push([op,args]));
  await runtime.classify({},{});
  await assert.rejects(runtime.generateImages({},{}), /provider/);
  const admitted=calls.filter(([op])=>op==='_budget').map(([,args])=>args.sequence);
  const completed=calls.filter(([op])=>op==='_request_done').map(([,args])=>args.sequence);
  assert.deepEqual(completed, admitted);
  assert.deepEqual(calls.filter(([op])=>op==='_request_done').map(([,args])=>args.failed), [false, true]);
});

test('Pi re-selection failures poison inference rather than using an uncertain model', async () => {
  const { installReselection, modelAuthority } = await import('../adapters/pi/model-runtime.mjs');
  for (const fault of ['prepare', 'unavailable', 'apply', 'projection', 'ack']) {
    let branch = [];
    let applying = false;
    const fallback = { provider: 'fixture', id: 'fallback' };
    const preferred = { provider: 'fixture', id: 'preferred' };
    const session = {
      model: fallback, thinkingLevel: 'off',
      sessionManager: { getBranch: () => branch },
      agent: { prepareNextTurnWithContext: async () => ({ context: { messages: ['completed-result'] } }) },
      async setModel(model) {
        if (applying && fault === 'apply') throw new Error('application failed');
        this.model = model;
        if (!(applying && fault === 'projection')) branch = [{ type: 'model_change', provider: model.provider, modelId: model.id }];
      },
      setThinkingLevel(level) { this.thinkingLevel = level; },
    };
    const authority = modelAuthority();
    const runtime = { getModel: (_provider, id) => id === 'fallback' ? fallback : fault === 'unavailable' ? null : preferred };
    const operations = [];
    const call = async (_bridge, operation) => {
      operations.push(operation);
      if (operation === '_pi_reselect') {
        if (fault === 'prepare') throw new Error('lost host response');
        return { switch: { provider: preferred.provider, model: preferred.id, authorization: 'exact' } };
      }
      throw new Error('lost commit response');
    };
    await installReselection(session, runtime, { provider: fallback.provider, model: fallback.id }, {}, authority, call);
    applying = true;
    await assert.rejects(session.agent.prepareNextTurnWithContext({}), undefined, fault);
    assert.throws(() => authority.check(session.model), /not host-authorized/);
    assert.deepEqual(operations, fault === 'ack' ? ['_pi_reselect', '_pi_reselect_applied'] : ['_pi_reselect']);
  }
});


test('Pi resume keeps matching transcript without a redundant model-change entry', async () => {
  const { installReselection, modelAuthority } = await import('../adapters/pi/model-runtime.mjs');
  const model = { provider: 'fixture', id: 'same' };
  const branch = [{ type: 'model_change', provider: model.provider, modelId: model.id }];
  let setModelCalls = 0;
  const session = { model, sessionManager: { getBranch: () => branch },
    agent: { prepareNextTurnWithContext: async () => ({}) },
    async setModel(next) { setModelCalls++; this.model = next; },
    setThinkingLevel(level) { this.thinkingLevel = level; } };
  const authority = modelAuthority();
  await installReselection(session, { getModel: () => model },
    { provider: model.provider, model: model.id }, {}, authority);
  authority.check(model);
  assert.equal(setModelCalls, 0);
  assert.equal(branch.length, 1);
});

test('Pi re-selection checks the SDK safe point before changing resumed history', async () => {
  const { installReselection, modelAuthority } = await import('../adapters/pi/model-runtime.mjs');
  const model = { provider: 'fixture', id: 'same' };
  let setModelCalls = 0;
  const session = { model, sessionManager: { getBranch: () => [] }, agent: {},
    async setModel(next) { setModelCalls++; this.model = next; },
    setThinkingLevel(level) { this.thinkingLevel = level; } };
  await assert.rejects(installReselection(session, { getModel: () => model },
    { provider: model.provider, model: model.id }, {}, modelAuthority()), /completed-result boundary/);
  assert.equal(setModelCalls, 0);
});

test('Pi re-selection refuses virtual models whose nested provider authority is not explicit', async () => {
  const { installReselection, modelAuthority } = await import('../adapters/pi/model-runtime.mjs');
  const virtual = { provider: 'fixture', id: 'virtual', api: 'pi-virtual' };
  const session = { model: virtual, sessionManager: { getBranch: () => [] },
    agent: { prepareNextTurnWithContext: async () => ({}) },
    async setModel(model) { this.model = model; } };
  const runtime = { getModel: () => virtual };
  const authority = modelAuthority();
  await assert.rejects(installReselection(session, runtime,
    { provider: virtual.provider, model: virtual.id }, {}, authority), /virtual models/);
  assert.throws(() => authority.check(virtual), /not host-authorized/);
});

test('authority sections preserve source order and replace only the Mizu namespace', () => {
  let hook;
  const pi = { on: (name, fn) => { if (name === 'before_agent_start') hook = fn; } };
  const policies = Array.from({ length: 12 }, (_, i) => [`mizu_authority_policy_${i}`, ` exact ${i}\r\n\n`]);
  const authority_sections = [...policies, ['mizu_authority_charter', ' exact charter\n']];
  // Canonical host JSON sorts object keys, but cannot reorder the pair array.
  register(pi, Type, { tools: [] }, { authoritySections: JSON.parse(JSON.stringify(authority_sections)) });
  const options = { sections: { unrelated: 'untouched', mizu_authority_policy_99: 'stale',
    mizu_authority_charter: 'old charter' } };
  hook({ systemPromptOptions: options });
  assert.deepEqual(Object.entries(options.sections), [['unrelated', 'untouched'], ...authority_sections]);
});


test('production Pi authority registration fails closed on missing or ambiguous sections', () => {
  const pi = { on() {} };
  assert.throws(() => register(pi, Type, { tools: [] }, { requireAuthority: true }), /authority sections/);
  for (const authority_sections of [
    [],
    [['mizu_authority_policy_0', 'policy']],
    [['mizu_authority_policy_1', 'policy'], ['mizu_authority_charter', 'charter']],
    [['mizu_authority_policy_0', 'policy'], ['mizu_authority_policy_0', 'duplicate'],
      ['mizu_authority_charter', 'charter']],
    [['mizu_authority_policy_0', 'policy'], ['mizu_authority_charter', 'charter'],
      ['mizu_authority_policy_1', 'out of order']],
  ]) {
    assert.throws(() => register(pi, Type, { tools: [] }, { authoritySections: authority_sections, requireAuthority: true }),
      /authority/);
  }
  let hook;
  const valid = [['mizu_authority_policy_0', 'policy'], ['mizu_authority_charter', 'charter']];
  register({ on: (name, fn) => { if (name === 'before_agent_start') hook = fn; } }, Type,
    { tools: [] }, { authoritySections: valid, requireAuthority: true });
  assert.equal(typeof hook, 'function');
});
