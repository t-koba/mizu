import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs/promises';
import { request, typeSchema } from '../adapters/pi/bridge-client.mjs';
import { register } from '../adapters/pi/register.mjs';

async function server(t, respond, timeout = 200) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'mizu-node-'));
  const socket = path.join(dir, 'bridge.sock');
  const peers = new Set();
  const listener = net.createServer(peer => {
    peers.add(peer); peer.on('error', () => {}); peer.on('close', () => peers.delete(peer));
    let text = '';
    peer.on('data', chunk => { text += chunk; if (text.includes('\n')) respond(peer, JSON.parse(text.split('\n')[0])); });
  });
  await new Promise((resolve, reject) => { listener.once('error', reject); listener.listen(socket, resolve); });
  t.after(async () => {
    for (const peer of peers) peer.destroy();
    await new Promise(resolve => listener.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  });
  return { socket, token: 'unit-test-token', timeout_ms: timeout };
}
const Type = Object.fromEntries(['Union', 'Literal', 'String', 'Integer', 'Boolean', 'Array', 'Object', 'Optional']
  .map(name => [name, (...args) => ({ name, args })]));

test('LF framing preserves Unicode separators and passes authenticated arguments', async t => {
  const cfg = await server(t, (peer, value) => {
    assert.equal(value.token, 'unit-test-token'); assert.equal(value.operation, 'read');
    assert.deepEqual(value.arguments, { path: 'a' });
    peer.end(JSON.stringify({ ok: true, result: 'a\u2028b\u2029c日本語' }) + '\n');
  });
  assert.equal(await request(cfg, 'read', { path: 'a' }), 'a\u2028b\u2029c日本語');
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
  const cfg = await server(t, peer => peer.end('x'.repeat(2097153)));
  await assert.rejects(request(cfg, 'read', {}), /byte limit/);
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
  const cfg = { protocol: 1, tools: [{ name: 'mizu_finish', operation: 'finish', description: 'End', inputSchema: { type: 'object', properties: {} } }] };
  register(pi, Type, cfg, { request: async (_cfg, operation, args) => { calls.push([operation, args]); return { recorded: true }; }, ...overrides });
  return { tools, events, active, calls };
}
test('extension enables only explicitly granted tools and authenticates startup', async () => {
  const r = registration(); await r.events.session_start();
  assert.deepEqual(r.active, ['mizu_finish']); assert.equal(r.calls[0][0], '_hello');
  assert.equal(r.tools[0].executionMode, 'sequential');
});
test('provider hook charges each admission with a monotonically increasing sequence', async () => {
  const r = registration(); await r.events.before_provider_request(); await r.events.before_provider_request();
  assert.deepEqual(r.calls, [['_budget', { sequence: 1 }], ['_budget', { sequence: 2 }]]);
});
test('budget refusal requests process termination instead of trusting hook exceptions', async () => {
  const exits = []; const r = registration({ request: async () => { throw Error('no budget'); }, exit: code => exits.push(code), diagnostic: () => {} });
  await r.events.before_provider_request(); assert.deepEqual(exits, [75]);
});
test('finish returns explicit loop termination', async () => {
  const r = registration(); const result = await r.tools[0].execute('id', {});
  assert.equal(result.terminate, true); assert.equal(JSON.parse(result.content[0].text).recorded, true);
});
test('unknown bridge protocol is rejected', () => {
  assert.throws(() => register({}, Type, { protocol: 999 }), /Unsupported/);
});
