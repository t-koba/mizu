/** Real installed Pi replay/serialization contract, with an offline faux endpoint.
 * See tests/pi-reasoning.md for scope. No Pi serializer is reimplemented here.
 */
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { meterRuntime, installReselection, modelAuthority, resolveThinkingLevel } from '../adapters/pi/model-runtime.mjs';
import { DEFAULT_THINKING_LEVEL as sdkDefaultThinkingLevel }
  from '../adapters/pi/node_modules/@earendil-works/pi-coding-agent/dist/core/defaults.js';
import { MODEL_THINKING_LEVELS }
  from '../adapters/pi/node_modules/@earendil-works/pi-ai/dist/providers/model-schema.js';
import { register } from '../adapters/pi/register.mjs';
import { buildBridgeTools } from '../adapters/pi-durable/launcher.mjs';

// Resolve each adapter's own installed copy, not an unrelated top-level SDK.
// These are the JS entry points named by the packages' public export maps.
const installed = (adapter, pkg, entry = 'index') => import(new URL(
  `../adapters/${adapter}/node_modules/@earendil-works/${pkg}/dist/${entry}.js`, import.meta.url));

const thinking = id => [
  { type: 'thinking', thinking: `  reasoning ${id}\nline two: 雪 🧭\tend  `, signature: `opaque-signature-${id}` },
  { type: 'thinking', thinking: '', signature: `opaque-empty-${id}` },
  { type: 'redacted_thinking', data: `opaque-redacted-${id}` },
];
const text = value => ({ type: 'text', text: value });
const reasoningBlocks = blocks => blocks.filter(b => ['thinking', 'redacted_thinking'].includes(b.type));

const bridgeTool = { name: 'mizu_probe', operation: 'contract_probe', description: 'Offline host tool',
  inputSchema: { type: 'object', properties: { marker: { type: 'string' } }, required: ['marker'] } };
const hostResult = { text: 'Distinctive host-tool result: 雪 🧭 / Mizu 1.3 boundary', value: 73013 };
const toolInput = { marker: 'host-call-73013' };

function assertHostOutput(payload) {
  const outputs = payload.messages.flatMap(m => Array.isArray(m.content) ? m.content : [])
    .filter(b => b.type === 'tool_result' && b.tool_use_id === 'probe-call');
  assert.equal(outputs.length, 1, 'host tool output appears once in the next actual provider request');
  assert.ok(!outputs[0].is_error, 'host tool succeeded');
  assert.equal(outputs[0].content, JSON.stringify(hostResult),
    'production adapter preserves distinctive host result text through the real SDK serializer');
}

function assertReplay(payload, expected) {
  const assistants = payload.messages.filter(m => m.role === 'assistant');
  assert.equal(assistants.length, expected.length, 'retained assistant count at faux provider');
  for (let i = 0; i < expected.length; i++) {
    assert.deepEqual(reasoningBlocks(assistants[i].content), reasoningBlocks(expected[i].blocks),
      `reasoning changed or missing for retained assistant ${expected[i].id}`);
  }
}

// Synthetic SSE is only the remote service response. Pi itself parses the
// response, persists normalized messages, builds replay context, and serializes
// the next HTTP request. The endpoint checks the final JSON, not onPayload input.
function sse(model, blocks) {
  const events = [{ type: 'message_start', message: { id: 'fixture-response', type: 'message',
    role: 'assistant', model, content: [], usage: { input_tokens: 10, output_tokens: 0 } } }];
  for (const [index, content_block] of blocks.entries()) {
    if (content_block.type === 'tool_use') {
      events.push({ type: 'content_block_start', index, content_block: { ...content_block, input: {} } },
        { type: 'content_block_delta', index,
          delta: { type: 'input_json_delta', partial_json: JSON.stringify(content_block.input) } });
    } else {
      events.push({ type: 'content_block_start', index, content_block });
    }
    events.push({ type: 'content_block_stop', index });
  }
  events.push({ type: 'message_delta', delta: {
    stop_reason: blocks.some(b => b.type === 'tool_use') ? 'tool_use' : 'end_turn', stop_sequence: null,
  }, usage: { output_tokens: 10 } }, { type: 'message_stop' });
  return new Response(events.map(e => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join(''),
    { headers: { 'content-type': 'text/event-stream' } });
}

async function endpoint(adapter, includeReasoning = true) {
  const ai = await installed(adapter, 'pi-ai');
  const { anthropicProvider } = await installed(adapter, 'pi-ai', 'providers/anthropic');
  const api = await installed(adapter, 'pi-ai', 'api/anthropic-messages');
  const catalog = anthropicProvider().getModels();
  // A test-only model reference, never production profile/model selection.
  const model = { ...catalog.find(m => m.reasoning), provider: 'replay-contract',
    baseUrl: 'https://replay.invalid', contextWindow: 100000, maxTokens: 2048 };
  assert.ok(model.id, 'installed Anthropic reasoning model is required');
  const allowedModels = new Set([model.id]);
  let expected = [];
  let pending;
  let summary = false;
  const emitted = [];
  const requests = [];
  const failures = [];
  const fetch = async (_url, init) => {
    try {
      const payload = JSON.parse(init.body);
      assert.ok(allowedModels.has(payload.model));
      requests.push(structuredClone(payload));
      assert.ok(summary || pending, 'unexpected provider request (no scripted response)');
      if (summary) {
        // Pi's serializeConversation() produces a user text prompt for a
        // summarizer, NOT assistant replay blocks. Thinking in that text is
        // deliberately outside this invariant (it may be omitted by Pi).
        assert.ok(payload.messages.every(m => m.role === 'user'), 'summary is a text request, not replay');
      } else {
        assertReplay(payload, expected);
      }
      const response = summary ? { id: 'summary', blocks: [text('Earlier work summarized.')] } : pending;
      pending = undefined;
      if (!summary) {
        emitted.push(response);
        expected.push(response);
      }
      return sse(payload.model, response.blocks);
    } catch (error) {
      failures.push(error);
      throw error; // Fail closed; never delegate to global fetch or a network.
    }
  };
  const options = opts => ({ ...opts, apiKey: 'test-only', fetch, maxRetries: 0 });
  const provider = {
    id: model.provider, name: 'Offline reasoning contract',
    auth: { apiKey: { resolve: async () => ({ auth: { apiKey: 'test-only' }, source: 'fixture' }) } },
    getModels: () => [model],
    stream: (m, c, o) => api.stream(m, c, options(o)),
    streamSimple: (m, c, o) => api.streamSimple(m, c, options(o)),
  };
  return {
    ai, model, provider, emitted, requests, allowModel: id => allowedModels.add(id),
    reply(id, extra = [text(`answer-${id}`)]) {
      assert.equal(pending, undefined);
      const response = { id, blocks: [...(includeReasoning ? thinking(id) : []), ...extra] };
      pending = response;
      return response;
    },
    summarize() { summary = true; pending = { id: 'summary', blocks: [text('Earlier work summarized.')] }; },
    retain(messages) {
      assert.equal(pending, undefined, 'summary request was actually sent');
      summary = false;
      // Determine only WHICH messages survived compaction from real context.
      // Expected reasoning remains the independently emitted original bytes.
      const ids = messages.filter(m => m.role === 'assistant').map(m => m.content
        .filter(b => b.type === 'text').map(b => b.text).join(''));
      expected = emitted.filter(e => ids.includes(e.blocks.filter(b => b.type === 'text').map(b => b.text).join('')));
      assert.ok(expected.length > 0, 'compaction must keep assistant history');
      assert.ok(expected.length < emitted.length, 'compaction must remove some assistant history');
      assert.equal(expected.length, ids.length, 'every retained assistant has an original fixture');
      return expected.length;
    },
    checked() {
      assert.equal(failures.length, 0, failures[0]?.message);
      assert.equal(pending, undefined, 'scripted provider response was consumed');
    },
  };
}

test('host-output assertion rejects missing, empty, changed and failed tool results', () => {
  const payload = { messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'probe-call',
    content: JSON.stringify(hostResult) }] }] };
  assert.doesNotThrow(() => assertHostOutput(payload));
  for (const mutate of [
    p => { p.messages = []; },
    p => { p.messages[0].content[0].content = ''; },
    p => { p.messages[0].content[0].content = 'changed'; },
    p => { p.messages[0].content[0].tool_use_id = 'wrong-call'; },
    p => { p.messages[0].content[0].is_error = true; },
  ]) {
    const altered = structuredClone(payload);
    mutate(altered);
    assert.throws(() => assertHostOutput(altered), assert.AssertionError);
  }
});

test('faux endpoint rejects dropped, changed, reordered and unsigned reasoning', () => {
  const original = { id: 'negative-control', blocks: [...thinking('negative-control'), text('answer')] };
  const payload = { messages: [{ role: 'assistant', content: structuredClone(original.blocks) }] };
  assert.doesNotThrow(() => assertReplay(payload, [original]));
  for (const mutate of [
    p => { p.messages = []; },
    p => { p.messages[0].content = [text('answer')]; },
    p => { p.messages[0].content[0].thinking = 'altered'; },
    p => { delete p.messages[0].content[0].signature; },
    p => { p.messages[0].content[2].data = 'altered'; },
    p => { p.messages[0].content.reverse(); },
  ]) {
    const altered = structuredClone(payload);
    mutate(altered);
    assert.throws(() => assertReplay(altered, [original]), assert.AssertionError);
  }
});

test('Pi production registration delivers host result text and replays retained history after real compaction', { timeout: 30000 }, async () => {
  const sdk = await installed('pi', 'pi-coding-agent');
  const directory = await mkdtemp(join(tmpdir(), 'mizu-reasoning-pi-'));
  let session;
  try {
    const remote = await endpoint('pi');
    const runtime = await sdk.ModelRuntime.create({ authPath: join(directory, 'auth.json'),
      modelsPath: null, modelsStorePath: join(directory, 'models.json'), refreshOnCreate: false });
    runtime.registerNativeProvider(remote.provider);
    const settingsManager = sdk.SettingsManager.inMemory({ compaction: { enabled: false, keepRecentTokens: 200 },
      retry: { enabled: false }, cacheWarming: 'off' });
    let tools = 0;
    const call = async (_config, operation, args) => {
      if (operation === '_hello') return {};
      assert.equal(operation, bridgeTool.operation);
      assert.deepEqual(args, toolInput);
      tools++;
      remote.reply('tool-answer');
      return hostResult;
    };
    const resourceLoader = new sdk.DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      systemPrompt: 'Offline reasoning contract.',
      extensionFactories: [{ name: 'mizu', factory: pi => register(pi, remote.ai.Type,
        { tools: [bridgeTool] }, { request: call }) }] });
    await resourceLoader.reload();
    const sessionManager = sdk.SessionManager.inMemory(directory);
    ({ session } = await sdk.createAgentSession({ cwd: directory, agentDir: directory, modelRuntime: runtime,
      model: remote.model, thinkingLevel: 'medium', settingsManager, resourceLoader, sessionManager,
      noTools: 'builtin', tools: [bridgeTool.name] }));
    for (const id of ['old-one', 'old-two']) {
      remote.reply(id, [text(`answer-${id} ${'old context '.repeat(600)}`)]);
      await session.prompt(`input-${id}`);
      remote.checked();
    }
    remote.reply('tool-call', [text('answer-tool-call'),
      { type: 'tool_use', id: 'probe-call', name: bridgeTool.name, input: toolInput }]);
    const requestsBeforeTool = remote.requests.length;
    await session.prompt('Use the host tool.');
    remote.checked();
    assert.equal(tools, 1);
    assert.equal(remote.requests.length, requestsBeforeTool + 2);
    assertHostOutput(remote.requests[requestsBeforeTool + 1]);

    remote.summarize();
    await session.compact(); // Real summary generation and SessionManager projection.
    const retained = remote.retain(sessionManager.buildSessionContext().messages);
    remote.checked();
    for (const id of ['after-compaction-one', 'after-compaction-two']) {
      remote.reply(id);
      await session.prompt(id);
      remote.checked();
    }
    assert.equal(remote.requests.at(-1).messages.filter(m => m.role === 'assistant').length, retained + 1);
  } finally {
    session?.dispose();
    await rm(directory, { recursive: true, force: true });
  }
});

test('Pi durable production tools deliver host result text, reopen a completed unit and compact history', { timeout: 30000 }, async () => {
  const sdk = await installed('pi-durable', 'pi-durable');
  const { openNodeSqliteStorage } = await installed('pi-durable', 'pi-durable', 'storage/sqlite/node');
  const { BACKGROUND_CONTEXT: context } = await installed('pi-durable', 'chord', 'context/index');
  const directory = await mkdtemp(join(tmpdir(), 'mizu-reasoning-durable-'));
  let harness;
  try {
    const remote = await endpoint('pi-durable');
    const models = remote.ai.createModels();
    models.setProvider(remote.provider);
    let tools = 0;
    const toolsForHost = buildBridgeTools({ tools: [bridgeTool] }, {}, { request: async (_bridge, operation, args) => {
      assert.equal(operation, bridgeTool.operation);
      assert.deepEqual(args, toolInput);
      tools++;
      remote.reply('tool-answer');
      return hostResult;
    } });
    const registry = sdk.createRegistry();
    registry.install(sdk.defineExtension({ name: 'replay-contract', tools: toolsForHost }));
    const open = async () => sdk.Harness.open(await openNodeSqliteStorage(join(directory, 'session.sqlite')),
      { models, registry, settings: { retry: { enabled: false }, compaction: { enabled: false, keepRecentTokens: 200 } } }, context);
    harness = await open();
    let root = await harness.root(context, { agent: { model: { provider: remote.model.provider, modelId: remote.model.id },
      instructions: 'Offline reasoning contract.', tools: toolsForHost } });
    const submit = async id => {
      const submission = await root.submit({ type: 'input', content: id, requestId: id }, context);
      const settled = await submission.wait(context);
      remote.checked();
      assert.equal(settled.status, 'done');
      return submission;
    };
    for (const id of ['old-one', 'old-two']) {
      remote.reply(id, [text(`answer-${id} ${'old context '.repeat(600)}`)]);
      await submit(id);
    }
    remote.reply('tool-call', [text('answer-tool-call'),
      { type: 'tool_use', id: 'probe-call', name: bridgeTool.name, input: toolInput }]);
    const requestsBeforeTool = remote.requests.length;
    const completed = await submit('tool-unit');
    assert.equal(tools, 1);
    assert.equal(remote.requests.length, requestsBeforeTool + 2);
    assertHostOutput(remote.requests[requestsBeforeTool + 1]);
    await harness.close(context);
    harness = await open();
    harness.resume();
    root = await harness.root(context);
    const requestsBefore = remote.requests.length;
    const duplicate = await root.submit({ type: 'input', content: 'tool-unit', requestId: 'tool-unit' }, context);
    assert.equal(duplicate.id, completed.id);
    assert.equal((await duplicate.wait(context)).status, 'done');
    assert.equal(remote.requests.length, requestsBefore, 'completed unit is not generated again');
    remote.reply('after-resume');
    await submit('after-resume');
    assert.equal(tools, 1, 'completed tool is not replayed on reopen');

    remote.summarize();
    const compacted = await harness.waitForTask(await root.compact(undefined, context), context);
    assert.equal(compacted.state.outcome.status, 'completed');
    const summaryWrite = await harness.submission(compacted.state.outcome.result.submissionId, context);
    assert.equal((await summaryWrite.wait(context)).status, 'done');
    const retained = remote.retain((await root.context(context)).messages);
    remote.checked();
    for (const id of ['after-compaction-one', 'after-compaction-two']) {
      remote.reply(id);
      await submit(id);
    }
    assert.equal(remote.requests.at(-1).messages.filter(m => m.role === 'assistant').length, retained + 1);
  } finally {
    await harness?.close(context);
    await rm(directory, { recursive: true, force: true });
  }
});

// Native catalog tests use the installed SDK, real reviewed extension loader and
// file store. Provider refresh is synthetic; no inference or external service.
import { writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { catalog, inspectNativeCatalog } from '../adapters/pi/catalog.mjs';

const nativeModel = { id: 'new-provider-only-model', name: 'Native fixture', provider: 'native-fixture',
  api: 'openai-completions', baseUrl: 'https://fixture.invalid', reasoning: false,
  input: ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 8192, maxTokens: 1024 };

async function nativeFixture(directory) {
  const extension = join(directory, 'provider.mjs');
  await writeFile(extension, `
    import { appendFileSync, existsSync, readFileSync } from 'node:fs';
    const trace = ${JSON.stringify(join(directory, 'refreshes.jsonl'))};
    const failure = ${JSON.stringify(join(directory, 'failure.txt'))};
    const model = ${JSON.stringify(nativeModel)};
    export default pi => {
      let models = [];
      pi.registerProvider({ id: 'unrelated-native', name: 'Other',
        auth: { apiKey: { name: 'Fixture', resolve: async () => undefined } }, getModels: () => [],
        async refreshModels(ctx) {
          appendFileSync(trace, JSON.stringify({ id: 'unrelated-native', network: ctx.allowNetwork })+'\\n');
          if (ctx.allowNetwork) throw new Error('unrelated network refresh forbidden');
        } });
      pi.registerProvider({ id: model.provider, name: 'Fixture',
        auth: { apiKey: { name: 'Fixture', resolve: async () => ({ auth: { apiKey: 'fixture-key' } }) } },
        getModels: () => models,
        async refreshModels(ctx) {
          appendFileSync(trace, JSON.stringify({ id: model.provider, network: ctx.allowNetwork, force: ctx.force })+'\\n');
          if (!ctx.allowNetwork) {
            if (ctx.stored) await ctx.publish({ update: () => { models = ctx.stored.models; } });
            return;
          }
          if (existsSync(failure)) {
            const code = readFileSync(failure, 'utf8');
            throw Object.assign(new Error('unsafe fixture exception'), { code });
          }
          await ctx.publish({ persist: { models: [model], checkedAt: 1234567800000 }, update: () => { models = [model]; } });
        },
        stream() { throw new Error('inference must not run'); },
        streamSimple() { throw new Error('inference must not run'); }
      });
    };
  `);
  return { provider: nativeModel.provider, model: 'pinned-old-model', agentDir: directory,
    cwd: directory, resources: [{ kind: 'extension', path: extension }], options: {}, refresh: false };
}

test('native catalog: real extension listing, provider-only cache, explicit selection and ordinary resolution', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-native-catalog-'));
  const originalFetch = globalThis.fetch;
  let fetches = 0;
  globalThis.fetch = () => { fetches++; throw new Error('network forbidden'); };
  try {
    const cfg = await nativeFixture(directory);
    const before = JSON.stringify(cfg);
    const sdk = await installed('pi', 'pi-coding-agent');
    const bundled = await sdk.ModelRuntime.create({ authPath: join(directory, 'auth.json'),
      modelsPath: null, allowModelNetwork: false });
    assert.equal(bundled.getModel(nativeModel.provider, nativeModel.id), undefined);
    const overlay = await inspectNativeCatalog(bundled, { ...cfg, provider: 'openai', refresh: true });
    assert.equal(overlay.status, 'unsupported');
    assert.equal(overlay.source, 'unsupported');
    assert.deepEqual(overlay.models, []);
    const empty = await catalog(cfg);
    assert.deepEqual(empty.models, []);
    assert.equal(empty.refresh, 'not_requested');
    const listed = await catalog({ ...cfg, refresh: true });
    assert.equal(listed.status, 'available');
    assert.equal(listed.source, 'registered_native_provider');
    assert.deepEqual(listed.models, [{ id: nativeModel.id, resolvable: true }]);
    const refreshes = (await readFile(join(directory, 'refreshes.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
    assert.deepEqual(refreshes.filter(row => row.network), [{ id: nativeModel.provider, network: true, force: true }]);
    assert.equal(listed.selected_model, 'pinned-old-model');
    assert.equal(JSON.stringify(cfg), before, 'inspection does not change the selection');
    assert.equal(listed.last_successful_observation, null);
    assert.equal(listed.freshness, 'unknown');
    assert.equal(listed.completeness, 'unknown');
    assert.deepEqual((await catalog(cfg)).models, listed.models, 'native hook restores on restart');
    for (const [code, expected] of [['ENETUNREACH', 'offline'], ['auth', 'auth_failed']]) {
      await writeFile(join(directory, 'failure.txt'), code);
      const failed = await catalog({ ...cfg, refresh: true });
      assert.equal(failed.error, expected);
      assert.equal(failed.refresh, 'failed');
      assert.deepEqual(failed.models, listed.models, 'real native hook retains restored cache on failure');
      assert.equal(failed.last_successful_observation, null);
    }

    // Explicit operator selection: exercise the ordinary launcher through RPC
    // get_state only. No bridge requests, prompts or inference are admitted.
    const effective = join(directory, 'effective.json');
    const bridge = join(directory, 'bridge.json');
    await writeFile(bridge, JSON.stringify({ tools: [] }));
    await writeFile(effective, JSON.stringify({ ...cfg, model: nativeModel.id,
      engine_tools: [], mcp_servers: {}, systemPrompt: 'No inference.', sessionDir: directory,
      authority_sections: [['mizu_authority_policy_0', 'No inference.'],
        ['mizu_authority_charter', 'Synthetic catalog-only authority.']] }));
    const child = spawnSync(process.execPath, ['adapters/pi/launcher.mjs', effective], {
      input: '{"id":"state","type":"get_state"}\n', encoding: 'utf8', timeout: 20000,
      env: { ...process.env, MIZU_BRIDGE_CONFIG: bridge }, maxBuffer: 1024 * 1024 });
    assert.equal(child.error, undefined);
    assert.equal(child.status, 0, child.stderr);
    const state = child.stdout.trim().split('\n').map(line => JSON.parse(line))
      .find(row => row.id === 'state');
    assert.equal(state.success, true, child.stdout);
    assert.equal(state.data.model.id, nativeModel.id);
    assert.equal(fetches, 0);
  } finally {
    globalThis.fetch = originalFetch;
    await rm(directory, { recursive: true, force: true });
  }
});

test('native catalog gates wrappers and filters refresh; incomplete, offline and auth reports are safe', async () => {
  const cfg = { provider: nativeModel.provider, model: 'pinned', refresh: true };
  const refreshes = [];
  const provider = { id: cfg.provider, getModels: () => [nativeModel], refreshModels() {} };
  const runtime = { getRegisteredNativeProvider: () => undefined,
    getModel: () => nativeModel, getModels: () => { throw new Error('merged listing forbidden'); },
    refresh: async options => { refreshes.push(options); return { aborted: false, errors: new Map() }; } };
  assert.equal((await inspectNativeCatalog(runtime, cfg)).status, 'unsupported');
  assert.deepEqual(refreshes, [], 'overlay-only builtin is never refreshed');
  runtime.getRegisteredNativeProvider = () => ({ ...provider, refreshModels: undefined });
  assert.equal((await inspectNativeCatalog(runtime, cfg)).status, 'unsupported');
  runtime.getRegisteredNativeProvider = () => provider;
  assert.equal((await inspectNativeCatalog(runtime, cfg)).status, 'available');
  assert.deepEqual(refreshes, [{ providers: [cfg.provider], allowNetwork: true, force: true }]);
  await inspectNativeCatalog(runtime, { ...cfg, refresh: false });
  assert.deepEqual(refreshes.at(-1), { providers: [cfg.provider], allowNetwork: false, force: false });
  for (const [code, expected] of [['ENETUNREACH', 'offline'], ['auth', 'auth_failed'], ['oauth', 'auth_failed'], ['secret', 'refresh_failed']]) {
    runtime.refresh = async () => ({ aborted: false,
      errors: new Map([[cfg.provider, Object.assign(new Error('unsafe fixture exception'), { code })]]) });
    const report = await inspectNativeCatalog(runtime, cfg);
    assert.equal(report.status, 'error');
    assert.equal(report.error, expected);
    assert.equal(report.refresh, 'failed');
    assert.deepEqual(report.models, [{ id: nativeModel.id, resolvable: true }], 'cached list survives failure');
    assert.ok(!JSON.stringify(report).includes('unsafe fixture exception'));
  }
  provider.getModels = () => [{ ...nativeModel, maxTokens: undefined }];
  const incomplete = await inspectNativeCatalog(runtime, cfg);
  assert.equal(incomplete.status, 'error', 'refresh error remains visible even with incomplete metadata');
  assert.equal(incomplete.models[0].resolvable, false);
  runtime.refresh = async () => ({ aborted: false, errors: new Map() });
  assert.equal((await inspectNativeCatalog(runtime, cfg)).status, 'incomplete');
  provider.getModels = () => [{ ...nativeModel, provider: 'wrong-source' }];
  assert.equal((await inspectNativeCatalog(runtime, cfg)).error, 'invalid_native_listing');
});

test('catalog launcher needs no bridge and never exposes setup diagnostics', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-catalog-cli-'));
  try {
    const cfg = await nativeFixture(directory);
    const effective = join(directory, 'effective.json');
    await writeFile(effective, JSON.stringify({ ...cfg, refresh: true }));
    const child = () => spawnSync(process.execPath, ['adapters/pi/launcher.mjs', '--model-catalog', effective],
      { encoding: 'utf8', timeout: 20000, env: { ...process.env, MIZU_BRIDGE_CONFIG: '' }, maxBuffer: 1024 * 1024 });
    const listed = child();
    assert.equal(listed.status, 0, listed.stderr);
    assert.equal(JSON.parse(listed.stdout).models[0].id, nativeModel.id);
    assert.equal(listed.stderr, '');
    await writeFile(cfg.resources[0].path, "export default () => { console.error('unsafe fixture diagnostic'); throw new Error('unsafe fixture exception'); };");
    const failed = child();
    assert.equal(failed.status, 0);
    assert.equal(JSON.parse(failed.stdout).error, 'provider_setup_failed');
    assert.equal(failed.stderr, '');
    assert.ok(!failed.stdout.includes('unsafe fixture'));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});


test('Pi selected thinking level uses profile overrides and the pinned SDK default', () => {
  const model = { provider: 'provider', id: 'model' };
  assert.equal(resolveThinkingLevel({ options: {} }, model), sdkDefaultThinkingLevel);
  for (const level of MODEL_THINKING_LEVELS) {
    assert.equal(resolveThinkingLevel({ options: { thinkingLevel: level } }, model), level,
      `runtime accepts pinned SDK thinking level ${level}`);
  }
  assert.equal(resolveThinkingLevel({ options: { settings: { defaultThinkingLevel: 'low' } } }, model), 'low');
  assert.equal(resolveThinkingLevel({ options: { settings: { defaultThinkingLevel: 'low',
    modelThinkingLevels: { 'provider/model': 'high' } } } }, model), 'high');
  assert.equal(resolveThinkingLevel({ options: { thinkingLevel: 'minimal',
    settings: { defaultThinkingLevel: 'low', modelThinkingLevels: { 'provider/model': 'high' } } } }, model), 'minimal');
  assert.throws(() => resolveThinkingLevel({ options: { thinkingLevel: 'not-a-level' } }, model),
    /Invalid selected Pi thinking level/);
});

test('Pi completed tool batch upgrades once, preserves effects/history, and reopens under host model authority', { timeout: 30000 }, async () => {
  const sdk = await installed('pi', 'pi-coding-agent');
  const directory = await mkdtemp(join(tmpdir(), 'mizu-reselection-pi-'));
  let session;
  try {
    const remote = await endpoint('pi', false);
    const preferred = { ...remote.model, id: 'preferred-fixture-model' };
    remote.allowModel(preferred.id);
    remote.provider.getModels = () => [remote.model, preferred];
    const runtime = await sdk.ModelRuntime.create({ authPath: join(directory, 'auth.json'),
      modelsPath: null, modelsStorePath: join(directory, 'models.json'), refreshOnCreate: false });
    runtime.registerNativeProvider(remote.provider);
    const authority = modelAuthority();
    const settingsManager = sdk.SettingsManager.inMemory({ compaction: { enabled: false },
      retry: { enabled: false }, cacheWarming: 'off' });
    let effects = 0;
    const operations = [];
    let manager;
    const call = async (_cfg, operation, args) => {
      operations.push(operation);
      if (operation === '_hello' || operation === '_model_usage' || operation === '_request_done' || operation === '_budget') return {};
      if (operation === bridgeTool.operation) {
        effects++;
        remote.reply('tool-answer');
        return hostResult;
      }
      if (operation === '_pi_reselect') {
        assert.equal(effects, 1, 'all host effects completed before selection');
        const results = manager.buildSessionContext().messages.filter(m => m.role === 'toolResult');
        assert.equal(results.length, 1, 'completed result persisted before selection');
        const durable = await readFile(manager.getSessionFile(), 'utf8');
        assert.ok(durable.includes('toolResult') && durable.includes('Distinctive host-tool result'),
          'same completed result is durable in the SDK file before host selection');
        if (switchToPreferred) {
          switchToPreferred = false;
          return { switch: { provider: preferred.provider, model: preferred.id,
            authorization: 'fixture-authorization', options: {} } };
        }
        return { switch: null };
      }
      assert.equal(operation, '_pi_reselect_applied');
      assert.deepEqual(args, { provider: preferred.provider, model: preferred.id, authorization: 'fixture-authorization' });
      assert.equal(session.model.id, preferred.id);
      assert.equal(session.thinkingLevel, sdkDefaultThinkingLevel);
      return { applied: true };
    };
    const meter = meterRuntime(runtime, {}, call, model => authority.check(model));
    const resourceLoader = new sdk.DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      systemPrompt: 'Offline re-selection contract.',
      extensionFactories: [{ name: 'mizu', factory: pi => register(pi, remote.ai.Type,
        { tools: [bridgeTool] }, { request: call }) }] });
    await resourceLoader.reload();
    manager = sdk.SessionManager.create(directory, directory);
    ({ session } = await sdk.createAgentSession({ cwd: directory, agentDir: directory, modelRuntime: runtime,
      model: remote.model, thinkingLevel: 'high', settingsManager, resourceLoader, sessionManager: manager,
      noTools: 'builtin', tools: [bridgeTool.name] }));
    const config = { provider: remote.model.provider, model: remote.model.id, options: { thinkingLevel: 'high' } };
    const resumeConfig = { provider: preferred.provider, model: preferred.id, options: {} };
    let switchToPreferred = true;
    await installReselection(session, runtime, config, {}, authority, call);
    remote.reply('tool-call', [text('answer-tool-call'),
      { type: 'tool_use', id: 'probe-call', name: bridgeTool.name, input: toolInput }]);
    await session.prompt('Use the host tool.');

    await meter.flush();
    remote.checked();
    assert.equal(effects, 1);
    assert.equal(remote.requests.length, 2, 'no inference/probe beyond the natural two requests');
    assert.equal(operations.filter(op => op === '_budget').length, 2);
    assert.equal(remote.requests[0].model, remote.model.id);
    assert.equal(remote.requests[1].model, preferred.id);
    assertHostOutput(remote.requests[1]);
    assert.equal(operations.filter(op => op === '_pi_reselect_applied').length, 1);
    const filename = manager.getSessionFile();
    const id = manager.getSessionId();
    session.dispose();
    manager = sdk.SessionManager.open(filename, directory, directory);
    assert.equal(manager.getSessionId(), id);
    const resumed = await sdk.createAgentSession({ cwd: directory, agentDir: directory, modelRuntime: runtime,
      model: remote.model, thinkingLevel: 'medium', settingsManager, resourceLoader, sessionManager: manager,
      noTools: 'builtin', tools: [bridgeTool.name] });
    assert.equal(resumed.modelFallbackMessage, undefined, 'exact host-selected model remains authoritative on family resume');
    ({ session } = resumed);
    // Simulate an older family transcript whose final profile left thinking high.
    // The new unit selects B, which omits thinkingLevel, so reconciliation must
    // apply the profile's effective SDK default before any inference.
    session.setThinkingLevel('high');
    session.dispose();
    manager = sdk.SessionManager.open(filename, directory, directory);
    const resumedOmitted = await sdk.createAgentSession({ cwd: directory, agentDir: directory, modelRuntime: runtime,
      model: preferred, settingsManager, resourceLoader, sessionManager: manager,
      noTools: 'builtin', tools: [bridgeTool.name] });
    assert.equal(resumedOmitted.session.thinkingLevel, 'high', 'SDK restores the prior transcript setting before host reconciliation');
    ({ session } = resumedOmitted);
    await installReselection(session, runtime, resumeConfig, {}, authority, call);
    assert.equal(session.thinkingLevel, sdkDefaultThinkingLevel, 'selected profile omission resolves to the pinned SDK default');
    session.dispose();

    // The next unit can still start under profile A and explicitly restore its
    // own level while adopting the same family history.
    manager = sdk.SessionManager.open(filename, directory, directory);
    const resumedA = await sdk.createAgentSession({ cwd: directory, agentDir: directory, modelRuntime: runtime,
      model: remote.model, thinkingLevel: 'high', settingsManager, resourceLoader, sessionManager: manager,
      noTools: 'builtin', tools: [bridgeTool.name] });
    assert.equal(resumedA.modelFallbackMessage, undefined, 'exact host-selected model remains available on family resume');
    ({ session } = resumedA);
    await installReselection(session, runtime, config, {}, authority, call);
    assert.equal(session.thinkingLevel, 'high');
    remote.reply('next-unit');
    await session.prompt('Continue without repeating the tool.');
    await meter.flush();
    remote.checked();
    assert.equal(remote.requests.at(-1).model, remote.model.id);
    assertHostOutput(remote.requests.at(-1));
    assert.equal(effects, 1);
    assert.equal(remote.requests.length, 3);
  } finally {
    session?.dispose();
    await rm(directory, { recursive: true, force: true });
  }
});

test('ordinary Pi authority and tools persist as positional deltas across file-backed resume and compaction',
  { timeout: 30000 }, async () => {
    const sdk = await installed('pi', 'pi-coding-agent');
    const directory = await mkdtemp(join(tmpdir(), 'mizu-authority-pi-'));
    let session;
    try {
      const remote = await endpoint('pi', false);
      const runtime = await sdk.ModelRuntime.create({ authPath: join(directory, 'auth.json'),
        modelsPath: null, modelsStorePath: join(directory, 'models.json'), refreshOnCreate: false });
      runtime.registerNativeProvider(remote.provider);
      const settingsManager = sdk.SettingsManager.inMemory({ compaction: { enabled: false, keepRecentTokens: 200 },
        retry: { enabled: false }, cacheWarming: 'off' });
      const addedTool = { ...bridgeTool, name: 'mizu_added', operation: 'added_probe' };
      let sessionFile, manager, executions = [], cwd;
      const oldSections = { mizu_authority_policy_0: '  old policy λ\r\n\n',
        mizu_authority_policy_1: 'obsolete policy\n', mizu_authority_charter: 'old charter' };
      const newSections = { mizu_authority_policy_0: '  new exact policy 雪\r\n\n',
        mizu_authority_charter: '  new charter\n\n' };
      const open = async (sections, tools) => {
        session?.dispose();
        // PiDriver gives each unit a fresh controller CWD while resuming the same session file.
        cwd = await mkdtemp(join(directory, 'controller-'));
        const resourceLoader = new sdk.DefaultResourceLoader({ cwd, agentDir: directory, settingsManager,
          noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
          systemPrompt: 'You are an AI assistant running in Mizu.',
          extensionFactories: [
            { name: 'unrelated', factory: pi => pi.on('before_agent_start', event => {
              event.systemPromptOptions.sections.unrelated_extension = 'keep unrelated section';
              // Also prove that Mizu replaces, not merges, its entire owned namespace.
              event.systemPromptOptions.sections.mizu_authority_stale = 'must be removed';
            }) },
            { name: 'mizu', factory: pi => register(pi, remote.ai.Type,
              { tools }, { authoritySections: Object.entries(sections), request: async (_cfg, operation) => {
                if (operation === '_hello') return {};
                assert.ok(tools.some(t => t.operation === operation), 'only current host tools may execute');
                executions.push(operation);
                remote.reply(`tool-answer-${executions.length}`);
                return hostResult;
              } }) },
          ] });
        await resourceLoader.reload();
        manager = sessionFile ? sdk.SessionManager.open(sessionFile, directory, cwd)
          : sdk.SessionManager.create(cwd, directory);
        ({ session } = await sdk.createAgentSession({ cwd, agentDir: directory, modelRuntime: runtime,
          model: remote.model, settingsManager, resourceLoader, sessionManager: manager,
          noTools: 'builtin', tools: tools.map(t => t.name) }));
        sessionFile = manager.getSessionFile();
      };
      const messages = () => manager.buildSessionContext().messages;
      const assertCurrent = sections => {
        const current = remote.ai.getCurrentSystemMessage(messages());
        assert.equal(current.sections.preamble, 'You are an AI assistant running in Mizu.');
        assert.equal(current.sections.unrelated_extension,
          '<unrelated_extension>\nkeep unrelated section\n</unrelated_extension>');
        assert.deepEqual(Object.keys(current.sections).filter(k => k.startsWith('mizu_authority_')),
          Object.keys(sections));
        for (const [name, value] of Object.entries(sections)) {
          assert.equal(current.sections[name], `<${name}>\n${value}\n</${name}>`);
        }
        // Use Pi AI itself to resolve both transport forms. Do not emulate
        // replay/collapse in the fixture: provider request assertions follow below.
        const collapsed = remote.ai.resolveTranscript({ messages: messages() }, false);
        assert.equal(collapsed.messages.filter(m => m.role === 'system').length, 1);
        assert.equal(remote.ai.getCurrentSystemPrompt(collapsed.messages),
          remote.ai.getCurrentSystemPrompt(messages()));
        const positional = remote.ai.resolveTranscript({ messages: messages() }, true);
        assert.deepEqual(positional.messages, messages());
      };
      const assertCwdCurrent = () => {
        assert.ok(remote.ai.getCurrentSystemMessage(messages()).sections.cwd.includes(cwd),
          'Pi current CWD section tracks this unit after prompt preparation');
      };
      await open(oldSections, [bridgeTool]);
      for (const id of ['authority-old-one', 'authority-old-two']) {
        remote.reply(id, [text(`answer-${id} ${'old context '.repeat(600)}`)]);
        await session.prompt(`input-${id}`);
        remote.checked();
      }
      assertCurrent(oldSections);
      assertCwdCurrent();
      const firstFile = sessionFile;
      const before = messages().filter(m => m.role === 'user');
      await open(newSections, [bridgeTool, addedTool]);
      assert.equal(sessionFile, firstFile, 'resume the same file, not a new session');
      assert.deepEqual(messages().filter(m => m.role === 'user'), before, 'createAgentSession retained user history');
      remote.reply('authority-new', [text('answer-authority-new'),
        { type: 'tool_use', id: 'added-call', name: addedTool.name, input: toolInput }]);
      await session.prompt('Use the newly added tool.');
      remote.checked();
      assert.deepEqual(executions, [addedTool.operation]);
      assertCurrent(newSections);
      assertCwdCurrent();
      assert.ok(messages().some(m => m.role === 'system' && m.sections?.mizu_authority_policy_1 === null),
        'obsolete source is removed by a positional system delta');
      assert.ok(messages().some(m => m.role === 'system' && m.toolsAdded?.some(t => t.name === addedTool.name)),
        'new tool declaration is persisted through the SDK tool path');
      const actualRequest = remote.requests.at(-1);
      const wire = JSON.stringify(actualRequest);
      assert.ok(wire.includes('new exact policy 雪'));
      assert.ok(wire.includes('input-authority-old-one'), 'provider still sees prior user history');
      // This installed provider supports positional system updates: the old
      // head stays cacheable and the exact replacement/removal reaches the wire.
      const update = actualRequest.messages.find(m => m.role === 'system');
      const updateText = update.content.filter(b => b.type === 'text').map(b => b.text).join('');
      for (const [name, value] of Object.entries(newSections)) {
        assert.ok(updateText.includes(`<${name}>\n${value}\n</${name}>`));
      }
      assert.ok(updateText.includes('Removed system prompt section "mizu_authority_policy_1"'));
      assert.ok(updateText.includes(cwd), 'provider request carries this unit CWD update');
      assert.ok(JSON.stringify(actualRequest.system).includes('old policy λ'));


      await open(newSections, [bridgeTool]);
      let removedErrors = 0;
      session.subscribe(event => {
        if (event.type === 'tool_execution_end' && event.toolName === addedTool.name) {
          assert.equal(event.isError, true);
          assert.match(JSON.stringify(event.result), /not found/);
          removedErrors++;
          remote.reply('removed-tool-refused');
        }
      });
      remote.reply('try-stale-tool', [text('answer-try-stale-tool'),
        { type: 'tool_use', id: 'stale-call', name: addedTool.name, input: toolInput }]);
      await session.prompt('Attempt the removed historical tool.');
      remote.checked();
      assert.equal(removedErrors, 1);
      assert.deepEqual(executions, [addedTool.operation], 'history never grants execution');
      assert.deepEqual(remote.ai.getCurrentTools(messages()).map(t => t.name), [bridgeTool.name]);
      assert.ok(messages().some(m => m.role === 'system' && m.toolsRemoved?.some(t => t.name === addedTool.name)));
      assert.ok(!remote.requests.at(-1).tools?.some(t => t.name === addedTool.name),
        'provider declarations use the current tool set after removal');

      remote.summarize();
      await session.compact();
      remote.retain(messages());
      remote.checked();
      assertCurrent(newSections);
      await open(newSections, [bridgeTool]);
      assertCurrent(newSections); // Compaction checkpoint survives actual disk reopen.
      remote.reply('after-authority-compaction');
      await session.prompt('Continue after reopen.');
      remote.checked();
      assertCurrent(newSections);
      assertCwdCurrent();
      const after = JSON.stringify(remote.requests.at(-1));
      assert.ok(after.includes('new exact policy 雪') && after.includes('new charter'));
      assert.ok(!after.includes('old policy λ') && !after.includes('obsolete policy'));
      assert.equal(sessionFile, firstFile);
      const disk = await readFile(sessionFile, 'utf8');
      assert.ok(disk.includes('compaction'), 'real compaction and system checkpoints are file backed');
    } finally {
      session?.dispose();
      await rm(directory, { recursive: true, force: true });
    }
  });
