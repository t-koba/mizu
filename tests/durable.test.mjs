/** pi-durable persistence/tool/recovery and launcher contracts. No inference, no network.
 *
 * Uses the pinned pi-durable 1.1.0 + pi-ai faux provider over file-backed
 * SQLite: idempotent requestId submit, exactly-once tool execution across
 * close/reopen with resume, committed usage, and replay-unsafe default.
 */
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:net';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join as joinPath } from 'node:path';
import { agentChanges, credentialsFromFile, meterModels, settlementFields, submitWithCurrentAgent, usageDelta }
  from '../adapters/pi-durable/launcher.mjs';
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
  const tools = [probe, finish];
  registry.install(defineExtension({ name: 'test', tools }));
  // Return the installed registrations: agent tools take the tool objects,
  // exactly as the launcher passes them — never bare names or undefined.
  return { models, faux, registry, tools, calls: () => calls, seen };
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

test('usage delta returns only bounded known monotone model counters', () => {
  const key = 'test-only/model';
  const snapshot = value => ({ models: { [key]: value } });
  const before = snapshot({ input: 10, output: 2, cacheRead: 4, cacheWrite: 3,
    totalTokens: 19, reasoning: 1, cacheWrite1h: 2, cost: { input: 0.25, total: 0.5 } });
  const after = snapshot({ input: 17, output: 5, cacheRead: 6, cacheWrite: 4,
    totalTokens: 32, reasoning: 2, cacheWrite1h: 4, cost: { input: 0.5, total: 0.75 } });
  const original = structuredClone([before, after]);
  assert.deepEqual(usageDelta(before, after, key), { input: 7, output: 3, cacheRead: 2,
    cacheWrite: 1, totalTokens: 13, reasoning: 1, cacheWrite1h: 2, cost: { input: 0.25, total: 0.25 } });
  assert.deepEqual([before, after], original); // pure; neither snapshot moves
  assert.deepEqual(usageDelta({ models: {} }, after, key), after.models[key]);
  assert.deepEqual(usageDelta(before, before, key), { input: 0, output: 0, cacheRead: 0,
    cacheWrite: 0, totalTokens: 0, reasoning: 0, cacheWrite1h: 0, cost: { input: 0, total: 0 } });
  assert.deepEqual(usageDelta(before, snapshot({ input: 12, output: 1, cacheRead: '8',
    reasoning: NaN, totalTokens: Infinity, cacheWrite: -1, cacheWrite1h: 2.5,
    cost: { total: Number.MAX_SAFE_INTEGER + 1 } }), key), { input: 2 });
  for (const bad of [undefined, null, {}, [], { models: null }, { models: [] }, snapshot(null), snapshot([])]) {
    assert.equal(usageDelta(bad, after, key), null);
    assert.equal(usageDelta(before, bad, key), null);
  }
  for (const bad of [undefined, null, '4', true, -1, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, 1.5]) {
    assert.equal(usageDelta(snapshot({ input: bad }), snapshot({ input: 10 }), key), null);
    assert.equal(usageDelta({ models: {} }, snapshot({ input: bad }), key), null);
  }
  assert.equal(usageDelta(snapshot({}), snapshot({ input: 10, cost: { total: 1 } }), key), null);
  assert.equal(usageDelta(before, { models: {} }, key), null);
  assert.equal(usageDelta(before, { models: { 'other/model': { input: 99 } } }, key), null);
});

test('two request IDs in reopened SQLite report dispatch deltas, not session totals', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-usage-'));
  try {
    const env = backend();
    const calls = [];
    const models = env.models;
    meterModels(models, {
      admit: async () => 1, report: async usage => { calls.push(structuredClone(usage)); },
    });
    const file = join(directory, 's.sqlite');
    const agent = { model: { provider: 'test-only', modelId: 'model' },
      tools: env.tools, instructions: 'Do work.', cwd: directory };
    const key = 'test-only/model';
    const one = await Harness.open(await openNodeSqliteStorage(file), { models, registry: env.registry }, context);
    env.faux.setResponses([fauxAssistantMessage('Work completed without finish.')]);
    const first = await submitWithCurrentAgent(one, agent,
      { type: 'input', content: 'work', requestId: 'unit' }, context);
    assert.equal((await first.submission.wait(context)).status, 'done');
    const firstTotal = await one.usage(context);
    const firstDelta = usageDelta(first.usageBefore, firstTotal, key);
    assert.deepEqual(firstDelta, calls[0]);
    assert.ok(firstDelta.totalTokens > 0);
    await one.close(context);

    const two = await Harness.open(await openNodeSqliteStorage(file), { models, registry: env.registry }, context);
    env.faux.setResponses([fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' })]);
    const second = await submitWithCurrentAgent(two, agent,
      { type: 'input', content: 'finish only', requestId: 'unit-t2' }, context);
    assert.notEqual(second.submission.id, first.submission.id);
    assert.deepEqual(second.usageBefore, firstTotal);
    assert.equal((await second.submission.wait(context)).status, 'done');
    const secondTotal = await two.usage(context);
    const secondDelta = usageDelta(second.usageBefore, secondTotal, key);
    assert.equal(calls.length, 2);
    assert.deepEqual(secondDelta, calls[1]); // independent per-request provider observation
    assert.ok(secondDelta.totalTokens > 0);
    assert.equal(firstDelta.totalTokens + secondDelta.totalTokens, secondTotal.models[key].totalTokens);
    assert.notDeepEqual(secondDelta, secondTotal.models[key]); // old + new must not be returned
    await two.close(context);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('normal no-finish return admits a distinct finish-only continuation in the same store', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    const env = backend();
    const file = join(directory, 's.sqlite');
    const one = await Harness.open(await openNodeSqliteStorage(file),
      { models: env.models, registry: env.registry }, context);
    const agent = { model: { provider: 'test-only', modelId: 'model' },
      tools: env.tools, instructions: 'Do work.', cwd: directory };
    env.faux.setResponses([
      fauxAssistantMessage([fauxToolCall('probe', {})], { stopReason: 'toolUse' }),
      fauxAssistantMessage('Work is complete, but no finish was called.'),
    ]);
    const { submission: first } = await submitWithCurrentAgent(one, agent,
      { type: 'input', content: 'work', requestId: 'unit' }, context);
    assert.equal((await first.wait(context)).status, 'done');
    assert.equal(env.calls(), 0);
    assert.equal(env.seen.length, 1);
    assert.equal(env.faux.state.callCount, 2);
    await one.close(context);

    // Like a second one-shot launcher: same SQLite store, but only finish is
    // registered and exposed. Configuring the retained agent must narrow it.
    const finishTools = env.tools.filter(tool => tool.name === 'finish');
    const registry = createRegistry();
    registry.install(defineExtension({ name: 'test', tools: finishTools }));
    const two = await Harness.open(await openNodeSqliteStorage(file),
      { models: env.models, registry }, context);
    const finishAgent = { ...agent, tools: finishTools };
    env.faux.setResponses([
      fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' }),
    ]);
    // Negative control: reusing the first request id cannot produce a finish.
    const duplicate = await submitWithCurrentAgent(two, finishAgent,
      { type: 'input', content: 'work', requestId: 'unit' }, context);
    assert.equal(duplicate.submission.id, first.id);
    assert.equal((await duplicate.submission.wait(context)).status, 'done');
    assert.equal(env.faux.state.callCount, 2);
    assert.equal(env.calls(), 0);
    const { root, submission: second } = await submitWithCurrentAgent(two, finishAgent,
      { type: 'input', content: 'finish only', requestId: 'unit-t2' }, context);
    assert.notEqual(second.id, first.id);
    assert.equal((await second.wait(context)).status, 'done');
    assert.deepEqual((await root.agent(context)).tools.map(tool => tool.name), ['finish']);
    assert.equal(env.faux.state.callCount, 3);
    assert.equal(env.calls(), 1);
    assert.equal(env.seen.length, 1);
    await two.close(context);
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

test('terminal unanswered replay is bound to requestId; a distinct id starts a new submission', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    const env = backend();
    const file = join(directory, 's.sqlite');
    const firstStorage = await openNodeSqliteStorage(file);
    const firstHarness = await Harness.open(firstStorage,
      { models: env.models, registry: env.registry }, context);
    const agent = { model: { provider: 'test-only', modelId: 'model' },
      tools: env.tools, instructions: 'Call probe; a later provider error is expected.', cwd: directory };
    const root = await firstHarness.root(context, { agent });
    env.faux.setResponses([
      fauxAssistantMessage([fauxToolCall('probe', {})], { stopReason: 'toolUse' }),
      fauxAssistantMessage('synthetic provider failure', {
        stopReason: 'error', errorMessage: 'synthetic provider failure' }),
    ]);
    const failed = await root.submit({ type: 'input', content: 'probe then fail', requestId: 'candidate-0' }, context);
    const settlement = await failed.wait(context);
    assert.equal(settlement.status, 'unanswered');
    assert.equal(settlement.reason, 'model_error');
    assert.equal(env.seen.length, 1);
    assert.equal(env.faux.state.callCount, 2);
    await firstHarness.close(context);

    // Reopen the same persistent store. resume must leave the completed
    // unanswered submission and its already-completed tool side effect alone.
    const secondStorage = await openNodeSqliteStorage(file);
    const secondHarness = await Harness.open(secondStorage,
      { models: env.models, registry: env.registry }, context);
    secondHarness.resume();
    const resumedRoot = await secondHarness.root(context);
    const duplicate = await resumedRoot.submit({ type: 'input', content: 'probe then fail',
      requestId: 'candidate-0' }, context);
    assert.equal(duplicate.id, failed.id);
    const replayed = await duplicate.wait(context);
    assert.equal(replayed.status, 'unanswered');
    assert.equal(replayed.reason, 'model_error');
    assert.equal(env.faux.state.callCount, 2); // same requestId admits no new provider call
    assert.equal(env.seen.length, 1); // neither submit nor resume replays the completed tool

    // The selector's next candidate has a new deterministic requestId, so
    // this is genuinely a fresh provider admission in the same store.
    env.faux.setResponses([
      fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' }),
    ]);
    const next = await resumedRoot.submit({ type: 'input', content: 'fallback candidate',
      requestId: 'candidate-0-a1' }, context);
    assert.equal((await next.wait(context)).status, 'done');
    assert.equal(env.faux.state.callCount, 3);
    assert.equal(env.seen.length, 1);
    await secondHarness.close(context);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('pi login store is shared file-backed with locked OAuth persistence', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-auth-'));
  try {
    const missing = join(directory, 'auth.json');
    const empty = await credentialsFromFile(missing);
    assert.equal(await empty.read('openai'), undefined);
    assert.deepEqual(await empty.list(), []);
    // A missing store keeps env-only behavior and never creates the file.
    assert.equal(existsSync(missing), false);
    const credential = { type: 'api_key', key: 'synthetic-test-only' };
    writeFileSync(missing, JSON.stringify({ 'test-only': credential }));
    const seeded = await credentialsFromFile(missing);
    assert.deepEqual(await seeded.read('test-only'), credential);
    assert.deepEqual(await seeded.list(), [{ providerId: 'test-only', type: 'api_key' }]);
    const oauth = { type: 'oauth', access: 'a', refresh: 'r', expires: Date.now() + 3600000 };
    writeFileSync(missing, JSON.stringify({ openai: oauth }));
    const subscription = await credentialsFromFile(missing);
    // OAuth entries are shared, not skipped: reads serve the stored
    // credential and modify persists the rotated replacement under Pi's
    // cross-process lock instead of discarding it (invalid_grant).
    assert.deepEqual(await subscription.read('openai'), oauth);
    assert.deepEqual(await subscription.list(), [{ providerId: 'openai', type: 'oauth' }]);
    const rotated = { type: 'oauth', access: 'a2', refresh: 'r2', expires: Date.now() + 7200000 };
    await subscription.modify('openai', async () => rotated);
    assert.deepEqual(await subscription.read('openai'), rotated);
    const persisted = JSON.parse(readFileSync(missing, 'utf8'));
    assert.deepEqual(persisted.openai, rotated);
    // api_key entries still read alongside an oauth entry.
    writeFileSync(missing, JSON.stringify({ openai: rotated, 'test-only': credential }));
    const mixed = await credentialsFromFile(missing);
    assert.deepEqual(await mixed.read('test-only'), credential);
    assert.deepEqual(await mixed.read('openai'), rotated);
    writeFileSync(missing, '{corrupt');
    await assert.rejects(credentialsFromFile(missing), /Invalid Pi auth store/);
    writeFileSync(missing, JSON.stringify({ 'test-only': { type: 'oauth', access: 'a' } }));
    await assert.rejects(credentialsFromFile(missing), /Invalid Pi auth credential/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('settlement fields preserve bounded provider detail', () => {
  const detail = '402: upstream request failed: insufficient account funds';
  assert.deepEqual(settlementFields({ status: 'unanswered', reason: 'model_error', detail }),
    { status: 'unanswered', reason: 'model_error', detail });
  const long = '😀'.repeat(4001);
  const bounded = settlementFields({ status: 'unanswered', reason: 'model_error', detail: long });
  assert.equal(Array.from(bounded.detail).length, 4000);
  assert.equal(bounded.reason, 'model_error');
});

test('agentChanges converges only what differs', async () => {
  const wanted = { model: { provider: 'p', modelId: 'm' }, instructions: 'new',
    cwd: '/work', tools: [{ name: 'a' }, { name: 'b' }] };
  const same = { model: { provider: 'p', modelId: 'm' }, instructions: 'new',
    cwd: '/work', tools: [{ name: 'a' }, { name: 'b' }] };
  assert.deepEqual(agentChanges(same, wanted), {});
  assert.deepEqual(agentChanges({ ...same, thinkingLevel: 'off' }, wanted), {});
  assert.deepEqual(agentChanges({ ...same, model: { provider: 'p', modelId: 'old' } }, wanted),
    { model: { provider: 'p', modelId: 'm' } });
  assert.deepEqual(agentChanges({ ...same, instructions: 'old' }, wanted), { instructions: 'new' });
  assert.deepEqual(agentChanges({ ...same, tools: [{ name: 'b' }, { name: 'a' }] }, wanted),
    { tools: wanted.tools });
  const drifted = { model: { provider: 'p', modelId: 'm' }, instructions: 'new',
    cwd: '/work', tools: [{ name: 'a', description: 'old' }, { name: 'b' }] };
  const current = { model: { provider: 'p', modelId: 'm' }, instructions: 'new',
    cwd: '/work', tools: [{ name: 'a', description: 'new' }, { name: 'b' }] };
  assert.deepEqual(agentChanges(drifted, current), { tools: current.tools });
  assert.deepEqual(agentChanges(null, wanted), { ...wanted });
  assert.deepEqual(agentChanges({ ...same, thinkingLevel: 'low' }, { ...wanted, thinkingLevel: 'high' }),
    { thinkingLevel: 'high' });
  assert.deepEqual(agentChanges({ ...same, thinkingLevel: 'high' }, wanted), { thinkingLevel: null });
  assert.deepEqual(agentChanges({ ...same, thinkingLevel: 'off' }, wanted), {});
});

// Bounded fake harness for launcher ordering, not an SDK recovery simulation.
function setupHarness(agent, { resolved = agent, applied = agent, failAt } = {}) {
  const calls = [];
  const changes = [];
  const inputs = [];
  const submission = { id: 'test-submission' };
  let reads = 0;
  async function step(name, ctx) {
    assert.equal(ctx, context);
    await Promise.resolve();
    calls.push(name);
    if (failAt === name) throw new Error(`synthetic ${name} failure`);
  }
  const root = {
    async agent(ctx) {
      assert.ok(++reads <= 2, 'agent reads stay bounded');
      await step(reads === 1 ? 'read' : 'readback', ctx);
      return reads === 1 ? resolved : applied;
    },
    async configure(change, ctx) {
      await step('configure', ctx);
      changes.push(change);
    },
    async submit(input, ctx) {
      await step('submit', ctx);
      inputs.push(input);
      return submission;
    },
  };
  const harness = {
    async root(ctx, options) {
      await step('root', ctx);
      assert.equal(options.agent, agent);
      return root;
    },
    async usage(ctx) {
      await step('usage', ctx);
      return { models: {} };
    },
    resume() { calls.push('resume'); },
  };
  return { harness, root, submission, calls, changes, inputs };
}

const currentAgent = { model: { provider: 'test-only', modelId: 'current' },
  instructions: 'Current instructions.', cwd: '/work',
  tools: [{ name: 'finish', description: 'Current grant', parameters: { type: 'object' } }] };
const retainedAgent = { model: { provider: 'test-only', modelId: 'old' },
  instructions: 'Old instructions.', cwd: '/old-work', tools: [{ name: 'removed' }] };
const currentInput = { type: 'input', content: 'go', requestId: 'same-store-request' };

test('launcher validates agent readback before resume and submit', { timeout: 1000 }, async () => {
  const fake = setupHarness(currentAgent, { resolved: retainedAgent });
  const result = await submitWithCurrentAgent(fake.harness, currentAgent, currentInput, context);
  assert.deepEqual(fake.calls, ['root', 'read', 'configure', 'readback', 'usage', 'resume', 'submit']);
  assert.deepEqual(fake.changes, [currentAgent]);
  assert.equal(fake.inputs[0], currentInput); // content and replay requestId pass through unchanged
  assert.equal(result.root, fake.root);
  assert.equal(result.submission, fake.submission);
});

test('launcher resumes after a current agent read without redundant configure', { timeout: 1000 }, async () => {
  const fake = setupHarness(currentAgent);
  await submitWithCurrentAgent(fake.harness, currentAgent, currentInput, context);
  assert.deepEqual(fake.calls, ['root', 'read', 'usage', 'resume', 'submit']);
  assert.deepEqual(fake.changes, []);
  assert.equal(fake.inputs[0], currentInput);
});

for (const [failAt, expected] of [
  ['root', ['root']],
  ['read', ['root', 'read']],
  ['configure', ['root', 'read', 'configure']],
  ['readback', ['root', 'read', 'configure', 'readback']],
]) {
  test(`launcher never resumes when ${failAt} fails`, { timeout: 1000 }, async () => {
    const fake = setupHarness(currentAgent, { resolved: retainedAgent, failAt });
    await assert.rejects(submitWithCurrentAgent(fake.harness, currentAgent, currentInput, context),
      new RegExp(`synthetic ${failAt} failure`));
    assert.deepEqual(fake.calls, expected); // neither resume nor submit is reached
    assert.deepEqual(fake.inputs, []);
  });
}

test('launcher preserves an immutable baseline before resume can work', async () => {
  const fake = setupHarness(currentAgent);
  const snapshot = { models: { 'test-only/current': { input: 5 } } };
  fake.harness.usage = async () => snapshot;
  fake.harness.resume = () => { snapshot.models['test-only/current'].input = 99; };
  const result = await submitWithCurrentAgent(fake.harness, currentAgent, currentInput, context);
  assert.deepEqual(result.usageBefore, { models: { 'test-only/current': { input: 5 } } });
});

test('failed usage snapshot stays unknown without changing submission semantics', async () => {
  const fake = setupHarness(currentAgent, { failAt: 'usage' });
  const result = await submitWithCurrentAgent(fake.harness, currentAgent, currentInput, context);
  assert.equal(result.usageBefore, null);
  assert.equal(result.submission, fake.submission);
  assert.deepEqual(fake.calls, ['root', 'read', 'usage', 'resume', 'submit']);
});

test('launcher never resumes when agent readback does not converge', { timeout: 1000 }, async () => {
  const fake = setupHarness(currentAgent, { resolved: retainedAgent, applied: retainedAgent });
  await assert.rejects(submitWithCurrentAgent(fake.harness, currentAgent, currentInput, context),
    /Durable session kept a stale agent/);
  assert.deepEqual(fake.calls, ['root', 'read', 'configure', 'readback']);
  assert.deepEqual(fake.inputs, []);
});

test('reopened store keeps the conversation while configure applies the current agent', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    const env = backend();
    const file = join(directory, 's.sqlite');
    const first = await openNodeSqliteStorage(file);
    const one = await Harness.open(first, { models: env.models, registry: env.registry }, context);
    const agentA = { model: { provider: 'test-only', modelId: 'model' },
      tools: env.tools, instructions: 'Call probe then finish.', cwd: directory };
    const root = await one.root(context, { agent: agentA });
    env.faux.setResponses([fauxAssistantMessage([fauxToolCall('probe', {})], { stopReason: 'toolUse' }),
      fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' })]);
    const submitted = await root.submit({ type: 'input', content: 'go', requestId: 'cfg-1' }, context);
    assert.equal((await submitted.wait(context)).status, 'done');
    await one.close(context);
    // Reopen the SAME store: root({agent}) alone must not rewrite the
    // retained agent (negative control for the configure path).
    const second = await openNodeSqliteStorage(file);
    const two = await Harness.open(second, { models: env.models, registry: env.registry }, context);
    two.resume();
    const agentB = { model: { provider: 'test-only', modelId: 'model' },
      tools: [env.tools[1]], instructions: 'Call finish only.', cwd: directory };
    await two.root(context, { agent: agentB });
    const retained = await (await two.root(context)).agent(context);
    assert.equal(retained.instructions, 'Call probe then finish.');
    assert.deepEqual(retained.tools.map((tool) => tool.name), ['probe', 'finish']);
    // Explicit configure converges the live conversation: instructions
    // update, the removed tool drops out, usage persists.
    const live = await two.root(context);
    await live.configure({ instructions: agentB.instructions, tools: agentB.tools }, context);
    const applied = await live.agent(context);
    assert.equal(applied.instructions, 'Call finish only.');
    assert.deepEqual(applied.tools.map((tool) => tool.name), ['finish']);
    env.faux.setResponses([fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' })]);
    const next = await live.submit({ type: 'input', content: 'again', requestId: 'cfg-2' }, context);
    assert.equal((await next.wait(context)).status, 'done');
    const usage = await two.usage(context);
    assert.ok(usage.models['test-only/model']);
    await two.close(context);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('metering guards entry points and stops past the turn bound', async () => {
  assert.throws(() => meterModels({}, { admit: async () => {}, report: async () => {} }),
    /No meterable provider entry point/);
  assert.throws(() => meterModels({ stream: async () => {} }, {}), /admit\/report hooks/);
  let dispatched = 0;
  let admitted = 0;
  const reported = [];
  let tripped = false;
  const fake = {
    stream: async () => {
      dispatched++;
      return { result: async () => ({ usage: { input: 1 } }),
        [Symbol.asyncIterator]() { return (async function* () {})(); } };
    },
  };
  meterModels(fake, {
    admit: async () => {
      if (++admitted > 1 && !tripped) {
        tripped = true;
        const error = new Error('Durable turn bound exhausted');
        error.code = 'TURN_BOUND';
        throw error;
      }
    },
    report: async (usage) => { reported.push(usage); },
  });
  await (await fake.stream()).result();
  assert.equal(dispatched, 1);
  await assert.rejects((await fake.stream()).result(), /Durable turn bound exhausted/);
  assert.equal(dispatched, 1); // the over-budget call never dispatches
  assert.ok(tripped);
  assert.deepEqual(reported, [{ input: 1 }]);
});

test('launcher refuses malformed compaction policy fail-closed', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    const base = { provider: 'p', model: 'm', instructions: 'i', store: join(directory, 's.sqlite'),
      requestId: 'r', prompt: 'go', cwd: directory, tools: [{ name: 't' }],
      authPath: join(directory, 'auth.json') };
    const launcher = joinPath(dirname(fileURLToPath(import.meta.url)), '..', 'adapters', 'pi-durable', 'launcher.mjs');
    // Partial triples pass through (the Harness merges over its built-ins);
    // only empty, unknown, or out-of-range shapes refuse.
    for (const compaction of [
      { reserveTokens: -1, keepRecentTokens: 20000, backgroundTokens: 32768 },
      {},
      { reserveTokens: 16384, keepRecentTokens: 20000, backgroundTokens: 32768, extra: 1 },
      { reserveTokens: '16384', keepRecentTokens: 20000, backgroundTokens: 32768 },
    ]) {
      const file = join(directory, `effective-${Math.random().toString(36).slice(2)}.json`);
      writeFileSync(file, JSON.stringify({ ...base, compaction }));
      const child = spawnSync(process.execPath, [launcher, file], { encoding: 'utf8' });
      assert.notEqual(child.status, 0);
      assert.match(child.stderr, /compaction/);
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('launcher runs without a turn bound; requests_per_run governs spend', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    // No max_turns anywhere: the launcher must not name a missing turn
    // bound. It still fails here, but on provider auth, not on max_turns.
    const cfg = { provider: 'p', model: 'm', instructions: 'i', store: join(directory, 's.sqlite'),
      requestId: 'r', prompt: 'go', cwd: directory, tools: [{ name: 't' }],
      authPath: join(directory, 'auth.json') };
    const file = join(directory, 'effective.json');
    writeFileSync(file, JSON.stringify(cfg));
    const launcher = joinPath(dirname(fileURLToPath(import.meta.url)), '..', 'adapters', 'pi-durable', 'launcher.mjs');
    const child = spawnSync(process.execPath, [launcher, file], { encoding: 'utf8' });
    assert.notEqual(child.status, 0);
    assert.doesNotMatch(child.stderr, /max_turns/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('launcher settles a model-admission failure from its process entrypoint', { timeout: 10000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-launcher-error-'));
  const operations = [];
  const server = createServer(socket => {
    let input = '';
    let handled = false;
    socket.on('data', chunk => {
      input += chunk.toString('utf8');
      const newline = input.indexOf('\n');
      if (handled || newline < 0) return;
      handled = true;
      const request = JSON.parse(input.slice(0, newline));
      operations.push(request.operation);
      socket.end(JSON.stringify({ ok: false, error: 'synthetic local admission refusal' }) + '\n');
    });
  });
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    const address = server.address();
    assert.ok(address && typeof address === 'object');
    const authPath = join(directory, 'auth.json');
    const bridgePath = join(directory, 'bridge.json');
    const configPath = join(directory, 'effective.json');
    writeFileSync(authPath, JSON.stringify({ openai: { type: 'api_key', key: 'offline-test-key' } }));
    writeFileSync(bridgePath, JSON.stringify({ transport: 'tcp', host: '127.0.0.1', port: address.port,
      timeout_ms: 2000, token: 'local-test', joined_operations: [] }));
    writeFileSync(configPath, JSON.stringify({ provider: 'openai', model: 'gpt-4o-mini',
      instructions: 'test', store: join(directory, 'store.sqlite'), requestId: 'offline-test',
      prompt: 'test', cwd: directory, authPath,
      tools: [{ name: 'finish', description: 'Seal', operation: 'finish',
        inputSchema: { type: 'object', properties: {}, required: [] } }] }));
    const launcher = joinPath(dirname(fileURLToPath(import.meta.url)), '..', 'adapters', 'pi-durable', 'launcher.mjs');
    const childEnv = { PATH: process.env.PATH ?? '', HOME: directory, USERPROFILE: directory,
      TMP: directory, TEMP: directory, MIZU_BRIDGE_CONFIG: bridgePath };
    for (const name of ['SystemRoot', 'WINDIR']) {
      if (process.env[name]) childEnv[name] = process.env[name];
    }
    const child = spawn(process.execPath, [launcher, configPath], {
      cwd: dirname(launcher), env: childEnv, stdio: ['ignore', 'pipe', 'pipe'], timeout: 8000,
    });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', chunk => { stdout += chunk; });
    child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
    const { code, signal } = await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('close', (exitCode, exitSignal) => resolve({ code: exitCode, signal: exitSignal }));
    });
    assert.equal(code, 0, stderr);
    assert.equal(signal, null);
    assert.ok(operations.length > 0);
    assert.ok(operations.every(operation => operation === '_budget'));
    const result = JSON.parse(stdout.trim());
    assert.equal(result.durable_result, true);
    assert.notEqual(result.status, 'done');
    assert.match(result.detail, /synthetic local admission refusal/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});

test('interrupted unsafe tool intent is not replayed on same-request recovery', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    const file = join(directory, 's.sqlite');
    const effect = join(directory, 'effect');
    const support = new URL('../adapters/pi-durable/test-support.mjs', import.meta.url).href;
    // Crash after the durable intent and an external side effect, but before
    // the tool result commit. No mocked tasks or SQLite writes.
    const child = spawnSync(process.execPath, ['--input-type=module', '-e', `
      import { writeFileSync } from 'node:fs';
      import { BACKGROUND_CONTEXT as context, Harness, Type, createModels, createRegistry,
        defineExtension, defineTool, fauxProvider, fauxAssistantMessage, fauxToolCall,
        openNodeSqliteStorage } from ${JSON.stringify(support)};
      const models = createModels();
      const faux = fauxProvider({ provider: 'test-only', models: [{ id: 'model',
        contextWindow: 32768, maxTokens: 1024 }] });
      models.setProvider(faux.provider);
      const probe = defineTool({ name: 'probe', description: 'Record one execution',
        parameters: Type.Object({}), execute: async () => {
          writeFileSync(${JSON.stringify(effect)}, 'executed once');
          process.exit(0);
        } });
      const registry = createRegistry();
      registry.install(defineExtension({ name: 'test', tools: [probe] }));
      const harness = await Harness.open(await openNodeSqliteStorage(${JSON.stringify(file)}),
        { models, registry }, context);
      const root = await harness.root(context, { agent: { model: { provider: 'test-only', modelId: 'model' },
        tools: [probe], instructions: 'Call probe.' } });
      faux.setResponses([fauxAssistantMessage([fauxToolCall('probe', {})], { stopReason: 'toolUse' })]);
      const submission = await root.submit({ type: 'input', content: 'go', requestId: 'uncertain' }, context);
      await submission.wait(context);
      process.exit(9); // Must exit from inside the tool instead.
    `], { encoding: 'utf8', timeout: 10000 });
    assert.equal(child.status, 0, child.stderr);
    assert.equal(readFileSync(effect, 'utf8'), 'executed once');
    const env = backend();
    const harness = await Harness.open(await openNodeSqliteStorage(file),
      { models: env.models, registry: env.registry }, context);
    env.faux.setResponses([
      fauxAssistantMessage([fauxToolCall('finish', {})], { stopReason: 'toolUse' }),
    ]);
    const agent = { model: { provider: 'test-only', modelId: 'model' },
      tools: env.tools, instructions: 'Call probe.' };
    const { submission } = await submitWithCurrentAgent(harness, agent,
      { type: 'input', content: 'go', requestId: 'uncertain' }, context);
    assert.equal((await submission.wait(context)).status, 'done');
    assert.equal(env.seen.length, 0); // Recovery reports interrupted, never executes probe again.
    assert.equal(env.calls(), 1);
    assert.equal(readFileSync(effect, 'utf8'), 'executed once');
    await harness.close(context);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('durable provider completion pairs admission without usage and on rejection', async () => {
  const done=[], admitted=[];
  let sequence=0;
  const fake={
    stream: async()=>({result:async()=>({}), async *[Symbol.asyncIterator]() {}}),
    classify: async()=>{throw Error('provider');},
  };
  meterModels(fake, {
    admit:async()=>{admitted.push(++sequence);return sequence;},
    report:async()=>{}, done:async (seq, failed)=>done.push([seq, failed]),
  });
  await fake.stream().result();
  await assert.rejects(fake.classify(), /provider/);
  assert.deepEqual(done.map(([seq])=>seq), admitted);
  assert.deepEqual(done.map(([, failed])=>failed), [false, true]);
});
