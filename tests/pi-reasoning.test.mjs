/** Real installed Pi replay/serialization contract, with an offline faux endpoint.
 * See tests/pi-reasoning.md for scope. No Pi serializer is reimplemented here.
 */
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
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

async function endpoint(adapter) {
  const ai = await installed(adapter, 'pi-ai');
  const { anthropicProvider } = await installed(adapter, 'pi-ai', 'providers/anthropic');
  const api = await installed(adapter, 'pi-ai', 'api/anthropic-messages');
  const catalog = anthropicProvider().getModels();
  // A test-only model reference, never production profile/model selection.
  const model = { ...catalog.find(m => m.reasoning), provider: 'replay-contract',
    baseUrl: 'https://replay.invalid', contextWindow: 100000, maxTokens: 2048 };
  assert.ok(model.id, 'installed Anthropic reasoning model is required');
  let expected = [];
  let pending;
  let summary = false;
  const emitted = [];
  const requests = [];
  const failures = [];
  const fetch = async (_url, init) => {
    try {
      const payload = JSON.parse(init.body);
      assert.equal(payload.model, model.id);
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
      return sse(model.id, response.blocks);
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
    ai, model, provider, emitted, requests,
    reply(id, extra = [text(`answer-${id}`)]) {
      assert.equal(pending, undefined);
      const response = { id, blocks: [...thinking(id), ...extra] };
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
      assert.equal(pending, undefined, 'scripted provider response was consumed');
      assert.equal(failures.length, 0, failures[0]?.message);
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
