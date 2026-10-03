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
async function server(t, respond, timeout = 200) {
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
  assert.deepEqual(await request({ transport: "unix", socket, token: 'unit-test-token', timeout_ms: 200 }, 'read', {}), {text:'unix-ok'});
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
  assert.equal((await r.events.tool_call({toolName:'ungranted',parentToolCallId:'parent'})).block,true);
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
