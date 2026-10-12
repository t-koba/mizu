/** Real pi-durable persistence/tool/recovery contracts. No inference, no network.
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

import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join as joinPath } from 'node:path';
import { credentialsFromFile, meterModels } from '../adapters/pi-durable/launcher.mjs';
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
      requestId: 'r', prompt: 'go', cwd: directory, tools: [{ name: 't' }], max_turns: 4,
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

test('launcher refuses effective configuration without max_turns', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-'));
  try {
    // Every field but max_turns: the gate must name the missing bound.
    const cfg = { provider: 'p', model: 'm', instructions: 'i', store: join(directory, 's.sqlite'),
      requestId: 'r', prompt: 'go', cwd: directory, tools: [{ name: 't' }] };
    const file = join(directory, 'effective.json');
    writeFileSync(file, JSON.stringify(cfg));
    const launcher = joinPath(dirname(fileURLToPath(import.meta.url)), '..', 'adapters', 'pi-durable', 'launcher.mjs');
    const child = spawnSync(process.execPath, [launcher, file], { encoding: 'utf8' });
    assert.notEqual(child.status, 0);
    assert.match(child.stderr, /max_turns/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('interrupted tools are never replayed by default', async () => {
  const tool = defineTool({ name: 'op', description: 'op', parameters: Type.Object({}), execute: async () => ({}) });
  assert.equal(tool.replay ?? 'unsafe', 'unsafe');
});
