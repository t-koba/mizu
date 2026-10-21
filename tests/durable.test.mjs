/** pi-durable persistence/tool/recovery and launcher contracts. No inference, no network.
 *
 * Uses the pinned pi-durable 1.3.0 + pi-ai faux provider over file-backed
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
import { agentChanges, assertProviderAuth, boundStatusFor, meterModels, resolveConfiguredModel, settlementFields, submitWithCurrentAgent, usageDelta }
  from '../adapters/pi-durable/launcher.mjs';
import { AssistantEntry, BACKGROUND_CONTEXT, Harness, Type, createModels, createRegistry,
  defineExtension, defineTool, fauxAssistantMessage, fauxProvider, fauxToolCall,
  openNodeSqliteStorage, openaiProvider } from '../adapters/pi-durable/test-support.mjs';

const context = BACKGROUND_CONTEXT;

// The launcher is a child of this process and its start-up is dominated by
// importing the Pi SDK, so a fixed number of milliseconds cannot say how
// long it may take: on a loaded host the launcher needs longer than the
// bound it was given, the child is killed before it can report, and the
// test reads an empty refusal or a null exit code instead of the
// launcher's answer. The bound is therefore this host's own measured
// start-up cost, scaled, with a floor and a ceiling that stays far below
// the check phase's own budget (600 s).
// Measured on this host: the launcher's start-up cost was 3.2-4.3 s idle
// and 8.9-44.5 s under 32-way load, a spread of about 14x. A floor of
// 30 s and a multiplier of 8 sat below that spread, so a child that
// needed 44.5 s was still killed before it could report. The floor now
// sits above the worst observed case with margin, and the multiplier
// scales a loaded sample well past it.
const LAUNCHER_OBSERVED_WORST_STARTUP_MS = 44500;
const LAUNCHER_OBSERVED_MARGIN_MS = 10000;
// The floor this host needed before: kept only so the regression can say
// that the old value was below the host's need.
const LAUNCHER_LEGACY_STARTUP_FLOOR_MS = 30000;
const LAUNCHER_STARTUP_FLOOR_MS = 60000;
const LAUNCHER_STARTUP_MULTIPLIER = 12;
const LAUNCHER_CHILD_CEILING_MS = 150000;
const LAUNCHER_TEST_MARGIN_MS = 15000;
// The measuring spawn carries its own ceiling: a host that cannot answer
// it leaves the floor in place instead of hanging the phase. It is the
// child's own ceiling, so the product and the harness measure the
// identical child under one ceiling and never derive two different
// bounds for one child's start-up.
const LAUNCHER_MEASURE_CEILING_MS = 150000;
const CHECK_PHASE_BUDGET_MS = 600000;

function launcherChildTimeoutMs(measuredStartupMs) {
  if (measuredStartupMs === null || !Number.isFinite(measuredStartupMs)) return LAUNCHER_STARTUP_FLOOR_MS;
  return Math.min(LAUNCHER_CHILD_CEILING_MS,
    Math.max(LAUNCHER_STARTUP_FLOOR_MS, Math.ceil(measuredStartupMs * LAUNCHER_STARTUP_MULTIPLIER)));
}

const launcherPath = () => joinPath(dirname(fileURLToPath(import.meta.url)), '..', 'adapters', 'pi-durable', 'launcher.mjs');

function launcherEnv(directory, bridgePath) {
  const env = { PATH: process.env.PATH ?? '', HOME: directory, USERPROFILE: directory,
    TMP: directory, TEMP: directory, MIZU_BRIDGE_CONFIG: bridgePath };
  for (const name of ['SystemRoot', 'WINDIR']) {
    if (process.env[name]) env[name] = process.env[name];
  }
  return env;
}

// A provider without configured credentials: the launcher refuses it by
// name, which is the launcher's first answer and its own start-up cost.
function writeNoAuthConfig(directory, requestId) {
  const bridgePath = join(directory, 'bridge.json');
  const configPath = join(directory, 'effective.json');
  writeFileSync(bridgePath, JSON.stringify({ transport: 'tcp', host: '127.0.0.1', port: 1,
    timeout_ms: 2000, token: 'local-test', joined_operations: [] }));
  writeFileSync(configPath, JSON.stringify({ provider: 'openai', model: 'gpt-4o-mini',
    instructions: 'test', store: join(directory, 'store.sqlite'), requestId,
    prompt: 'test', cwd: directory, authPath: join(directory, 'auth.json'),
    modelsPath: join(directory, 'models.json'),
    tools: [{ name: 'finish', description: 'Seal', operation: 'finish',
      inputSchema: { type: 'object', properties: {}, required: [] } }] }));
  return { bridgePath, configPath };
}

async function measureLauncherStartupMs() {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-startup-'));
  try {
    const { bridgePath, configPath } = writeNoAuthConfig(directory, 'offline-startup');
    const started = Date.now();
    const child = spawn(process.execPath, [launcherPath(), configPath], {
      cwd: dirname(launcherPath()), env: launcherEnv(directory, bridgePath),
      stdio: ['ignore', 'pipe', 'pipe'], timeout: LAUNCHER_MEASURE_CEILING_MS });
    child.stdout?.resume();
    child.stderr?.resume();
    const code = await new Promise((resolve) => {
      child.once('error', () => resolve(null));
      child.once('close', (exitCode) => resolve(exitCode));
    });
    // The refusal is the launcher's first answer: it exits 1 by design.
    // Anything else means the measurement could not be taken.
    if (code !== 1) return null;
    const elapsed = Date.now() - started;
    return Number.isFinite(elapsed) && elapsed > 0 ? elapsed : null;
  } catch {
    return null;
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

// Measured once per file: the bounds below are this host's own.
const launcherMeasuredStartupMs = await measureLauncherStartupMs();
const LAUNCHER_CHILD_TIMEOUT_MS = launcherChildTimeoutMs(launcherMeasuredStartupMs);
const LAUNCHER_TEST_TIMEOUT_MS = LAUNCHER_CHILD_TIMEOUT_MS + LAUNCHER_TEST_MARGIN_MS;

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

test('the pinned model resolves from the engine directory models.json', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-models-'));
  try {
    const modelsPath = join(directory, 'models.json');
    const authPath = join(directory, 'auth.json');
    writeFileSync(modelsPath, JSON.stringify({ providers: { 'test-only': {
      name: 'Test Only', api: 'openai-completions', baseUrl: 'https://models.example.invalid/v1',
      models: [{ id: 'model-x', name: 'Model X',
        cost: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0 } }] } } }));
    // An operator-defined model resolves the way the pi engine resolves it:
    // the bundled catalog plus the engine directory's models.json.
    const resolved = await resolveConfiguredModel(
      { authPath, modelsPath, provider: 'test-only', model: 'model-x' });
    assert.equal(resolved.model.id, 'model-x');
    assert.equal(resolved.model.baseUrl, 'https://models.example.invalid/v1');
    assert.equal(resolved.model.cost.input, 1);
    assert.equal(resolved.model.cost.output, 2);
    // An unknown model still refuses loudly instead of guessing.
    await assert.rejects(
      resolveConfiguredModel({ authPath, modelsPath, provider: 'test-only', model: 'absent' }),
      /unavailable on pi-durable: test-only\/absent/);
    // A missing catalog leaves the bundled catalog in force: a built-in
    // provider still resolves, and no file is created for it.
    const empty = join(directory, 'empty');
    const builtins = await resolveConfiguredModel(
      { authPath, modelsPath: join(empty, 'models.json'), provider: 'openai', model: 'gpt-4o-mini' });
    assert.equal(builtins.model.id, 'gpt-4o-mini');
    assert.equal(existsSync(join(empty, 'models.json')), false);
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

test('the launcher reports the bound the handshake names', () => {
  assert.equal(boundStatusFor(new Error('Drain bound refused the provider request')), 'drain');
  assert.equal(boundStatusFor(new Error('Per-run provider request budget exhausted')),
    'request_bound');
  // Any other admission failure keeps its own path.
  assert.equal(boundStatusFor(new Error('daily model-request budget exhausted')), null);
  assert.equal(boundStatusFor(undefined), null);
});

test('the durable auth gate accepts a provider authenticated by an environment key', async () => {
  const models = createModels();
  models.setProvider(openaiProvider());
  const previous = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = 'offline-test-key';
  try {
    // The gate admits the provider: an environment key is a configured
    // credential, so the durable engine does not refuse the submission.
    await assertProviderAuth(models, 'openai');
  } finally {
    if (previous === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previous;
  }
  await assert.rejects(assertProviderAuth(models, 'test-only'),
    /Provider auth is not configured for test-only/);
});

test('the launcher child bounds follow this host cost', () => {
  // The bound is this host's measured start-up cost, scaled. A fixed
  // number of milliseconds cannot say how long the launcher may take:
  // the old fixed bound killed the child before it could report, and the
  // test then read an empty refusal instead of the launcher's answer.
  assert.equal(launcherChildTimeoutMs(null), LAUNCHER_STARTUP_FLOOR_MS);
  assert.equal(launcherChildTimeoutMs(0), LAUNCHER_STARTUP_FLOOR_MS);
  assert.equal(launcherChildTimeoutMs(10000), 10000 * LAUNCHER_STARTUP_MULTIPLIER);
  assert.equal(launcherChildTimeoutMs(1000000), LAUNCHER_CHILD_CEILING_MS);
  assert.equal(LAUNCHER_CHILD_TIMEOUT_MS, launcherChildTimeoutMs(launcherMeasuredStartupMs));
  assert.ok(LAUNCHER_CHILD_TIMEOUT_MS >= LAUNCHER_STARTUP_FLOOR_MS);
  assert.ok(LAUNCHER_CHILD_TIMEOUT_MS <= LAUNCHER_CHILD_CEILING_MS);
  assert.ok(LAUNCHER_TEST_TIMEOUT_MS > LAUNCHER_CHILD_TIMEOUT_MS);
  // The launcher's own bridge bound still decides its bridge work; the
  // child's bound only decides when a child that never answers is killed.
  assert.ok(LAUNCHER_TEST_TIMEOUT_MS < CHECK_PHASE_BUDGET_MS);
});

test('the launcher child bound stays above this host observed cost', () => {
  // Measured on this host: the launcher's start-up cost was 3.2-4.3 s idle
  // and 8.9-44.5 s under 32-way load. A bound below that need kills the
  // child before it can report, and the test then reads the empty refusal
  // or the null exit code it was meant to detect.
  assert.ok(LAUNCHER_OBSERVED_WORST_STARTUP_MS > 0);
  // The floor sits above the worst observed case, with margin.
  assert.ok(LAUNCHER_STARTUP_FLOOR_MS
    >= LAUNCHER_OBSERVED_WORST_STARTUP_MS + LAUNCHER_OBSERVED_MARGIN_MS);
  // The previous floor was below the host's need, which is how a child
  // could still be killed before it reported.
  assert.ok(LAUNCHER_LEGACY_STARTUP_FLOOR_MS
    < LAUNCHER_OBSERVED_WORST_STARTUP_MS + LAUNCHER_OBSERVED_MARGIN_MS);
  // A loaded sample scales well past the observed worst case.
  assert.ok(LAUNCHER_STARTUP_MULTIPLIER * LAUNCHER_OBSERVED_WORST_STARTUP_MS
    > LAUNCHER_OBSERVED_WORST_STARTUP_MS + LAUNCHER_OBSERVED_MARGIN_MS);
  // The bound derived from that sample, and the one this host measured,
  // both stay between the floor and the ceiling.
  const loaded = launcherChildTimeoutMs(LAUNCHER_OBSERVED_WORST_STARTUP_MS);
  assert.ok(loaded >= LAUNCHER_STARTUP_FLOOR_MS);
  assert.ok(loaded <= LAUNCHER_CHILD_CEILING_MS);
  assert.ok(LAUNCHER_CHILD_TIMEOUT_MS >= LAUNCHER_STARTUP_FLOOR_MS);
  assert.ok(LAUNCHER_CHILD_TIMEOUT_MS <= LAUNCHER_CHILD_CEILING_MS);
  // The phase's own budget holds when the ceiling applies: one measuring
  // spawn and the two test spawns, each with the test's own margin.
  assert.ok(LAUNCHER_MEASURE_CEILING_MS + 2 * LAUNCHER_TEST_TIMEOUT_MS
    <= CHECK_PHASE_BUDGET_MS);
});

test('the durable launcher refuses a provider without configured auth', { timeout: LAUNCHER_TEST_TIMEOUT_MS }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'mizu-durable-noauth-'));
  try {
    // No login store and no environment key: the provider has no auth.
    const { bridgePath, configPath } = writeNoAuthConfig(directory, 'offline-test');
    const launcher = launcherPath();
    const childEnv = launcherEnv(directory, bridgePath);
    const child = spawn(process.execPath, [launcher, configPath], {
      cwd: dirname(launcher), env: childEnv, stdio: ['ignore', 'pipe', 'pipe'],
      timeout: LAUNCHER_CHILD_TIMEOUT_MS });
    let stderr = '';
    child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
    const { code } = await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('close', (exitCode) => resolve({ code: exitCode }));
    });
    assert.notEqual(code, 0);
    assert.match(stderr, /Provider auth is not configured for openai/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('launcher settles a model-admission failure from its process entrypoint', { timeout: LAUNCHER_TEST_TIMEOUT_MS }, async () => {
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
      prompt: 'test', cwd: directory, authPath, modelsPath: join(directory, 'models.json'),
      tools: [{ name: 'finish', description: 'Seal', operation: 'finish',
        inputSchema: { type: 'object', properties: {}, required: [] } }] }));
    const launcher = joinPath(dirname(fileURLToPath(import.meta.url)), '..', 'adapters', 'pi-durable', 'launcher.mjs');
    const childEnv = { PATH: process.env.PATH ?? '', HOME: directory, USERPROFILE: directory,
      TMP: directory, TEMP: directory, MIZU_BRIDGE_CONFIG: bridgePath };
    for (const name of ['SystemRoot', 'WINDIR']) {
      if (process.env[name]) childEnv[name] = process.env[name];
    }
    const child = spawn(process.execPath, [launcher, configPath], {
      cwd: dirname(launcher), env: childEnv, stdio: ['ignore', 'pipe', 'pipe'],
      timeout: LAUNCHER_CHILD_TIMEOUT_MS,
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
