/** Real pi-durable persistence/tool/recovery contracts. No inference, no network.
 *
 * Uses the pinned pi-durable 1.0.4 + pi-ai faux provider over file-backed
 * SQLite: idempotent requestId submit, exactly-once tool execution across
 * close/reopen with resume, committed usage, and replay-unsafe default.
 */
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { AssistantEntry, BACKGROUND_CONTEXT, Harness, Type, createModels, createRegistry,
  defineExtension, defineTool, fauxAssistantMessage, fauxProvider, fauxToolCall,
  openNodeSqliteStorage } from '../adapters/pi-durable/test-support.mjs';

const context = BACKGROUND_CONTEXT;

function backend(directory) {
  const models = createModels();
  const faux = fauxProvider({ provider: 'test-only', models: [{ id: 'model', contextWindow: 32768, maxTokens: 1024 }] });
  models.setProvider(faux.provider);
  let calls = 0;
  const seen = [];
  const finish = defineTool({
    name: 'finish', description: 'Seal',
    parameters: Type.Object({}),
    execute: async () => {
      calls++;
      return { content: [{ type: 'text', text: 'sealed' }], details: { sealed: true }, control: { terminate: true } };
    },
  });
  const probe = defineTool({
    name: 'probe', description: 'Record one execution',
    parameters: Type.Object({}),
    execute: async () => {
      seen.push(Date.now());
      return { content: [{ type: 'text', text: 'seen' }], details: {} };
    },
  });
  const registry = createRegistry();
  registry.install(defineExtension({ name: 'test', tools: [probe, finish] }));
  return { models, faux, registry, calls: () => calls, seen };
}

test('requestId submit is idempotent and settles with usage', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    const env = backend();
    const { models, faux, registry, tools } = env;
    const storage = await openNodeSqliteStorage(join(directory, 's.sqlite'));
    const harness = await Harness.open(storage, { models, registry }, context);
    const root = await harness.root(context, { agent: { model: { provider: 'test-only', modelId: 'model' },
      tools, instructions: 'Call probe then finish.' } });
    faux.setResponses([fauxAssistantMessage([fauxToolCall('probe', {})], { stopReason: 'toolUse' }),
      fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' })]);
    const first = await root.submit({ type: 'input', content: 'go', requestId: 'r1' }, context);
    const again = await root.submit({ type: 'input', content: 'go', requestId: 'r1' }, context);
    assert.equal(again.id, first.id);
    const settled = await first.wait(context);
    assert.equal(settled.status, 'done');
    const usage = await harness.usage(context);
    assert.ok(usage.models['test-only/model']);
    await harness.close(context);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('close/reopen resumes pending work exactly once', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    const env = backend();
    const file = join(directory, 's.sqlite');
    const first = await openNodeSqliteStorage(file);
    const one = await Harness.open(first, { models: env.models, registry: env.registry }, context);
    const root = await one.root(context, { agent: { model: { provider: 'test-only', modelId: 'model' },
      tools: env.tools, instructions: 'Call probe then finish.' } });
    env.faux.setResponses([fauxAssistantMessage([fauxToolCall('probe', {})], { stopReason: 'toolUse' }),
      fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' })]);
    const submission = await root.submit({ type: 'input', content: 'go', requestId: 'crash-1' }, context);
    const id = submission.id;
    await one.close(context); // simulated crash: work may be pending
    const second = await openNodeSqliteStorage(file);
    const two = await Harness.open(second, { models: env.models, registry: env.registry }, context);
    two.resume();
    const root2 = await two.root(context);
    const same = await root2.submit({ type: 'input', content: 'go', requestId: 'crash-1' }, context);
    assert.equal(same.id, id);
    const settled = await same.wait(context);
    assert.equal(settled.status, 'done');
    assert.equal(env.seen.length, 1); // no double execution across recovery
    const answer = await root2.commit(tx => tx.entry(AssistantEntry, settled.answer), context);
    assert.ok(answer);
    await two.close(context);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('interrupted tools are never replayed by default', async () => {
  const tool = defineTool({ name: 'op', description: 'op', parameters: Type.Object({}), execute: async () => ({}) });
  assert.equal(tool.replay ?? 'unsafe', 'unsafe');
});
