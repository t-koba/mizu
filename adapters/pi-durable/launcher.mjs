/** Managed pi-durable entry point: real durable Harness over file-backed SQLite.
 *
 * One-shot protocol: `node launcher.mjs <effective.json>` runs one admitted
 * submission to settlement and prints a single JSON result document to
 * stdout (stderr carries progress only). No RPC, no JSONL event stream.
 *
 * Durable contracts (from @earendil-works/pi-durable 1.3.0):
 * - openNodeSqliteStorage(file): WAL SQLite; commits survive process crashes.
 * - Harness.open(storage, {models, registry, settings}): one Session line of
 *   commits. `settings.compaction` carries the operator live-context bound
 *   (reserve/keepRecent/background tokens); enabled stays true, absent keeps
 *   the engine/provider default with no Mizu value injected.
 * - root.submit({type:'input', content, requestId}): retried requestId
 *   returns the existing submission instead of submitting twice.
 * - submission.wait(): resolves done/unanswered. Interrupted tool calls are
 *   never rerun (default replay unsafe): the model gets an `interrupted`
 *   result, matching the Python driver's unknown-completion rule.
 * - harness.resume(): continue work a dead process left unfinished.
 * - harness.usage(): cumulative per provider/model + per tool spend.
 * - ModelRuntime resolves the pinned model from the bundled catalog plus the
 *   engine directory's models.json/cache. Unlike pi, native provider
 *   extensions are not loaded; model-catalog inspection is unsupported and
 *   diagnoses configured extension resources explicitly.
 *
 * Authority preserved from the pi engine: exact provider/model pin resolved
 * from the shared bundled/explicit sources (plus the engine directory's
 * models.json/cache, not Pi's native provider extensions), bridge tools only (native host tools refused
 * Python-side), per-request budget admission and usage reporting around every
 * provider call, mizu_finish seal required.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { Type } from '@earendil-works/pi-ai';
import { ModelRuntime } from '@earendil-works/pi-coding-agent';
import { BACKGROUND_CONTEXT } from '@earendil-works/chord/context';
import { AssistantEntry, createRegistry, defineExtension, defineTool, Harness } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { request, typeSchema } from './bridge-client.mjs';

const CONTRACT_EXPORTS = ['Harness', 'createRegistry', 'defineExtension', 'defineTool', 'openNodeSqliteStorage', 'ModelRuntime'];
const MAX_SETTLEMENT_DETAIL_CHARS = 4000;

/**
 * Resolve the pinned model from Pi's own sources.
 *
 * Schema: `cfg.authPath` (the shared engine-directory login store) and
 * `cfg.modelsPath` (the engine directory's models.json); the returned
 * `models` is Pi's model runtime and `model` the pinned definition.
 * Trust: operator-owned engine state read host-side by the launcher.
 * Failure: an unknown model refuses loudly with its provider and model;
 * a malformed catalog refuses loudly.
 */
export async function resolveConfiguredModel(cfg) {
  if (typeof cfg.authPath !== 'string' || !cfg.authPath) throw new Error('Effective configuration lacks authPath');
  if (typeof cfg.modelsPath !== 'string' || !cfg.modelsPath) throw new Error('Effective configuration lacks modelsPath');
  const models = await ModelRuntime.create({ authPath: cfg.authPath, modelsPath: cfg.modelsPath,
    modelsStorePath: join(dirname(cfg.modelsPath), 'models-cache.json'), allowModelNetwork: false });
  const model = models.getModel(cfg.provider, cfg.model);
  if (!model) throw new Error(`Configured exact model is unavailable on pi-durable: ${cfg.provider}/${cfg.model}`);
  return { models, model };
}

/**
 * Name the bound a shared admission refusal carries.
 *
 * The handshake message names the bound the host was given, so the launcher
 * reports that bound instead of always the request budget. Any other
 * admission failure keeps its own path.
 */
export function boundStatusFor(error) {
  const message = error && typeof error.message === 'string' ? error.message : '';
  if (message.includes('Drain bound refused the provider request')) return 'drain';
  if (message.includes('Per-run provider request budget exhausted')) return 'request_bound';
  return null;
}

/**
 * Refuse a provider without configured credentials.
 *
 * The shared login store and the environment keys are the only sources. A
 * provider with neither never reaches a model: the refusal names the provider
 * instead of the request budget, so a drained unit is not mistaken for a
 * spent budget.
 */
export async function assertProviderAuth(models, provider) {
  const auth = await models.checkAuth(provider).catch(() => undefined);
  if (!auth) throw new Error(`Provider auth is not configured for ${provider}`);
  return auth;
}

export function meterModels(models, hooks) {
  // Guarded provider metering shared by the one-shot submission path.
  // Each entry point is wrapped only when the pinned SDK provides it as a
  // function; a renamed or dropped method fails closed here instead of
  // crashing mid-run on undefined. Not shared with the pi engine's
  // meterRuntime: that wraps the Runtime (model, context, options) facade
  // with pi-virtual admission dedup, while this wraps the Models facade
  // where every stream* call is one provider request.
  if (!hooks || typeof hooks.admit !== 'function' || typeof hooks.report !== 'function') {
    throw new Error('Durable metering requires admit/report hooks');
  }
  const streams = ['stream', 'streamSimple', 'streamDeferred']
    .filter(name => typeof models?.[name] === 'function');
  if (!streams.length) throw new Error('No meterable provider entry point on pi-durable models');
  for (const name of streams) {
    const original = models[name].bind(models);
    models[name] = (...args) => {
      let sequence;
      const source = (async () => { sequence = await hooks.admit(); return original(...args); })();
      const pending = source.then(stream => stream.result()).then(async value => {
        await hooks.report(value?.usage, value);
        return value;
      }).then(async value => {
        if (sequence !== undefined) await hooks.done?.(sequence, false);
        return value;
      }, async error => {
        if (sequence !== undefined) await hooks.done?.(sequence, true);
        throw error;
      });
      // The rejection stays visible to result()/iterator consumers; this
      // empty branch only stops an unconsumed stream from crashing the
      // process after the bound already tripped.
      pending.catch(() => {});
      return {
        result: () => pending,
        async *[Symbol.asyncIterator]() {
          const stream = await source;
          yield* stream;
          await pending;
        },
      };
    };
  }
  for (const name of ['classify', 'generateImages']
    .filter(name => typeof models?.[name] === 'function')) {
    const original = models[name].bind(models);
    models[name] = async (...args) => {
      const sequence = await hooks.admit();
      let failed = true;
      try {
        const result = await original(...args);
        await hooks.report(result?.usage, result);
        failed = false;
        return result;
      } finally {
        if (sequence !== undefined) await hooks.done?.(sequence, failed);
      }
    };
  }
}

function fail(message) {
  process.stderr.write(`pi-durable: ${message}\n`);
  process.exit(1);
}

const invokedAsMain = process.argv[1] !== undefined
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (process.argv.includes('--check-contract') && invokedAsMain) {
  const missing = [];
  for (const [name, fn] of [
    ['Harness', Harness?.open], ['createRegistry', createRegistry],
    ['defineExtension', defineExtension], ['defineTool', defineTool],
    ['openNodeSqliteStorage', openNodeSqliteStorage], ['ModelRuntime', ModelRuntime?.create],
  ]) if (typeof fn !== 'function') missing.push(name);
  if (missing.length) fail(`Required durable capability missing: ${missing.join(', ')}`);
  process.stdout.write(JSON.stringify({ contract: 'managed-pi-durable-sdk', backend: 'pi-durable',
    exports: CONTRACT_EXPORTS, inference: 'not_run' }) + '\n');
} else if (invokedAsMain) {
  let cfg;
  try {
    cfg = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  } catch (error) {
    fail(`Cannot read effective configuration: ${error.message}`);
  }
  // Python owns absolute request/wall deadlines and bounded process cleanup.
  // A launcher-wide timer would cancel an admitted child during its join.
  try {
    process.stdout.write(JSON.stringify(await runOnce(cfg)) + '\n');
  } catch (error) {
    fail(error?.message || String(error));
  }
}

/** Build the same bridge tools for the launcher and installed-SDK contracts.
 * Only bridge I/O and finish notification are injectable; result mapping stays here.
 */
export function buildBridgeTools(cfg, bridge, { request: call = request, onFinish = () => {} } = {}) {
  return cfg.tools.map(tool => defineTool({
    name: tool.name,
    description: tool.description,
    parameters: typeSchema(Type, tool.inputSchema),
    execute: async (args) => {
      const result = await call(bridge, tool.operation, args);
      // Pi durable 1.3 uses output for model-facing blocks (coding Pi uses content).
      if (tool.operation === 'finish') {
        onFinish(result);
        return { output: [{ type: 'text', text: JSON.stringify(result) }], details: result,
          control: { terminate: true } };
      }
      return { output: [{ type: 'text', text: JSON.stringify(result) }], details: result };
    },
  }));
}

/** Preserve only bounded textual settlement detail in the one-shot result.
 * Provider error text is evidence, not a status code; Python pairs it with the
 * unchanged settlement reason for configured error predicates.
 */
function boundedSettlementDetail(value) {
  if (typeof value !== 'string') return undefined;
  let end = 0;
  let characters = 0;
  while (end < value.length && characters < MAX_SETTLEMENT_DETAIL_CHARS) {
    const point = value.codePointAt(end);
    end += point > 0xffff ? 2 : 1;
    characters++;
  }
  return value.slice(0, end);
}

export function settlementFields(settled) {
  const status = settled?.status;
  const detail = boundedSettlementDetail(settled?.detail);
  return {
    status,
    reason: status === 'done' ? null : (settled?.reason ?? 'unanswered'),
    ...(detail === undefined ? {} : { detail }),
  };
}

// Pure agent diff for in-place session updates (unit-tested below).
// Returns the AgentChange that converges the resolved agent to the wanted
// one, or {} when they already agree. Anything uncomparable converges by
// applying the wanted values: the operator's current configuration wins.
export function agentChanges(resolved, wanted) {
  const change = {};
  try {
    if (!resolved || typeof resolved !== 'object') return { ...wanted };
    if (resolved?.model?.provider !== wanted?.model?.provider
        || resolved?.model?.modelId !== wanted?.model?.modelId) {
      change.model = { provider: wanted.model.provider, modelId: wanted.model.modelId };
    }
    if ((resolved?.instructions ?? null) !== (wanted?.instructions ?? null)) {
      change.instructions = wanted.instructions ?? null;
    }
    if ((resolved?.cwd ?? null) !== (wanted?.cwd ?? null)) {
      change.cwd = wanted.cwd ?? null;
    }
    // Tools converge by value (name, description, parameters), not by
    // name alone, so bridge definition drift applies too. Uncomparable
    // shapes converge by applying the current registrations.
    const shape = (tool) => {
      if (!tool || typeof tool !== 'object' || typeof tool.name !== 'string') return null;
      let parameters = null;
      if (tool.parameters !== undefined) {
        try {
          parameters = JSON.stringify(tool.parameters);
        } catch {
          return null;
        }
      }
      return JSON.stringify({ name: tool.name, description: tool.description ?? null, parameters });
    };
    const have = Array.isArray(resolved?.tools) ? resolved.tools.map(shape) : null;
    const want = Array.isArray(wanted?.tools) ? wanted.tools.map(shape) : null;
    if (have === null || want === null
        || have.length !== want.length || have.some((entry, index) => entry === null || entry !== want[index])) {
      change.tools = wanted.tools ?? [];
    }
    if (wanted?.thinkingLevel !== undefined) {
      if (resolved?.thinkingLevel !== wanted.thinkingLevel) change.thinkingLevel = wanted.thinkingLevel;
    } else if (resolved?.thinkingLevel !== undefined && resolved?.thinkingLevel !== null
        && resolved?.thinkingLevel !== 'off') {
      // A removed override clears instead of retaining the old value.
      change.thinkingLevel = null;
    }
  } catch {
    return { ...wanted };
  }
  return change;
}

/** Per-dispatch fallback from cumulative Harness usage snapshots for one model.
 * Only known, monotone counters are returned (tokens: safe integers; cost:
 * finite numbers, both bounded by MAX_SAFE_INTEGER). Missing/malformed reads,
 * counters and regressions stay unknown, not zero. The one known-zero baseline
 * is an absent model in a valid initial models map. Optional counters missing
 * from an existing model are not assumed zero. Unknown fields are ignored.
 */
export function usageDelta(before, after, key) {
  const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  if (!record(before?.models) || !record(after?.models)
      || !Object.hasOwn(after.models, key) || !record(after.models[key])) return null;
  const initial = !Object.hasOwn(before.models, key);
  const previous = initial ? {} : before.models[key];
  if (!record(previous)) return null;
  const current = after.models[key];
  const subtract = (old, next, fields, integer) => {
    const result = {};
    const valid = value => typeof value === 'number' && Number.isFinite(value)
      && value >= 0 && value <= Number.MAX_SAFE_INTEGER && (!integer || Number.isSafeInteger(value));
    for (const field of fields) {
      const start = initial ? 0 : (record(old) && Object.hasOwn(old, field) ? old[field] : undefined);
      const end = record(next) && Object.hasOwn(next, field) ? next[field] : undefined;
      if (valid(start) && valid(end) && end >= start) result[field] = end - start;
    }
    return result;
  };
  const delta = subtract(previous, current,
    ['input', 'output', 'cacheRead', 'cacheWrite', 'cacheWrite1h', 'reasoning', 'totalTokens'], true);
  const cost = subtract(previous.cost, current.cost, ['input', 'output', 'cacheRead', 'cacheWrite', 'total'], false);
  if (Object.keys(cost).length) delta.cost = cost;
  return Object.keys(delta).length ? delta : null;
}

/** Validate the current agent before unpausing recovery in the same session. */
export async function submitWithCurrentAgent(harness, agent, input, context) {
  const root = await harness.root(context, { agent });
  const resolved = await root.agent(context);
  // Session continuity: model, tools, instructions, and cwd ride the live
  // conversation. A reopened session keeps its stored agent until told
  // otherwise, so apply the current submission's agent when it differs
  // (positional system entries keep prompt caches warm) instead of
  // refusing. New conversations already carry these values from root().
  const change = agentChanges(resolved, agent);
  if (Object.keys(change).length) {
    await root.configure(change, context);
    // Fail closed: read back the retained conversation before recovery can
    // dispatch any unfinished work.
    const applied = await root.agent(context);
    if (Object.keys(agentChanges(applied, agent)).length) {
      throw new Error('Durable session kept a stale agent after configure');
    }
  }
  // An unchanged first read is already the convergence check. In either case,
  // do not enable recovered work until current authority is confirmed.
  // Snapshot before resume can dispatch recovered work, not merely before
  // submit. Clone so a mutable provider/SDK result cannot move the baseline.
  let usageBefore = null;
  try { usageBefore = structuredClone(await harness.usage(context)); } catch { /* unknown */ }
  harness.resume(); // continue work a dead process left unfinished
  const submission = await root.submit(input, context);
  return { root, submission, usageBefore };
}

async function runOnce(cfg) {
  for (const field of ['provider', 'model', 'instructions', 'store', 'requestId', 'prompt', 'cwd']) {
    if (typeof cfg[field] !== 'string' || !cfg[field]) throw new Error(`Effective configuration lacks ${field}`);
  }
  if (!Array.isArray(cfg.tools) || !cfg.tools.length) throw new Error('Effective configuration carries no bridge tools');
  // Live-context bound: operator compaction thresholds ride the Harness
  // policy (read at every resolution, so retuning keeps the conversation).
  // Absent keeps the engine/provider default: no Mizu value is injected, so
  // a future SDK default change applies without a Mizu release. A partial
  // triple passes through; the Harness merges it over its built-ins.
  // Malformed refuses loudly. Enabled stays true: disabling automatic
  // compaction is not exposed, so live context stays bounded instead of
  // failing at overflow.
  const COMPACTION_MAX = 1073741824;
  let compaction = undefined;
  if (cfg.compaction !== undefined) {
    if (typeof cfg.compaction !== 'object' || cfg.compaction === null || Array.isArray(cfg.compaction)) {
      throw new Error('Effective configuration carries an invalid compaction policy');
    }
    const keys = Object.keys(cfg.compaction);
    const allowed = ['reserveTokens', 'keepRecentTokens', 'backgroundTokens'];
    if (!keys.length || keys.length > allowed.length || !keys.every(k => allowed.includes(k))) {
      throw new Error('Effective configuration carries an invalid compaction policy');
    }
    compaction = {};
    for (const key of keys) {
      const value = cfg.compaction[key];
      if (!Number.isSafeInteger(value) || value < 0 || value > COMPACTION_MAX) {
        throw new Error(`Effective configuration carries an invalid compaction policy: ${key}`);
      }
      compaction[key] = value;
    }
  }
  // cacheRetention: fail closed before any bridge/auth work so a malformed
  // profile refuses even offline.
  if (cfg.cacheRetention !== undefined
      && cfg.cacheRetention !== 'none' && cfg.cacheRetention !== 'short'
      && cfg.cacheRetention !== 'long') {
    throw new Error('Effective configuration carries an invalid cacheRetention');
  }
  if (cfg.engine_tools?.length) throw new Error('Native host tools bypass the OCI bridge');
  if (cfg.resources?.length) throw new Error('Resource kinds are unsupported on pi-durable');
  if (cfg.mcp_servers && Object.keys(cfg.mcp_servers).length) throw new Error('MCP servers are unsupported on pi-durable');
  const bridge = JSON.parse(readFileSync(process.env.MIZU_BRIDGE_CONFIG, 'utf8'));

  // Exact-model pin over the same source the pi engine uses: Pi's bundled
  // catalog plus the engine directory's models.json, resolved by Pi's own
  // model runtime. An operator-defined model therefore behaves the same on
  // both engines, and an unknown model still refuses loudly.
  const { models, model } = await resolveConfiguredModel(cfg);
  // Credentials resolve like pi: the shared engine-directory login store
  // (authPath) plus environment API keys. Pi's own file-backed store
  // persists OAuth refreshes under its cross-process lock, so a rotating
  // refresh token is never spent without write-back.
  await assertProviderAuth(models, cfg.provider);

  // Admission + usage metering around every provider call, mirroring the
  // pi engine: one _budget reservation before dispatch, one _model_usage
  // report after. complete* delegate to stream*, so wrapping the three
  // stream entry points meters each provider request exactly once.
  let sequence = 0;
  let requests = 0;
  let boundTripped = false;
  let drainTripped = false;
  let submission = null;
  const observations = [];
  async function admit() {
    const admittedSequence = ++sequence;
    try {
      await request(bridge, '_budget', { sequence: admittedSequence });
    } catch (error) {
      // The single per-unit spend bound (requests_per_run) is reached:
      // stop Harness work instead of running unbounded inside one
      // submission. wait() below settles the aborted submission; the flag
      // maps it to request_bound. Any other admission failure keeps its
      // own path (a daily-budget refusal still defers via admission_wait).
      const status = boundStatusFor(error);
      if (!boundTripped && status) {
        boundTripped = true;
        // The bound kind travels with the shared handshake, so the result
        // names the bound the host was given instead of always the request
        // budget.
        drainTripped = status === 'drain';
        try { if (submission) await submission.abort(context); } catch {}
      }
      throw error;
    }
    requests++;
    return admittedSequence;
  }
  async function reportUsage(usage, shape) {
    if (!usage) return;
    observations.push({ provider: shape?.provider ?? cfg.provider, model: shape?.model ?? cfg.model,
      thinkingLevel: shape?.thinkingLevel ?? cfg.thinkingLevel ?? null });
    await request(bridge, '_model_usage', { sequence: ++sequence, usage,
      model: { provider: cfg.provider, model: cfg.model, thinkingLevel: cfg.thinkingLevel ?? null } });
  }
  meterModels(models, { admit, report: reportUsage,
    done: (sequence, failed = false) => request(bridge, '_request_done', { sequence, failed }) });

  // Bridge tools as one durable extension. Intent commits before execute;
  // interrupted executions are never rerun (replay unsafe default), so an
  // uncertain side effect surfaces as `interrupted`, never a blind repeat.
  let finishCalled = false;
  let sealed = null;
  const tools = buildBridgeTools(cfg, bridge, { onFinish: result => {
    finishCalled = true;
    sealed = result;
  } });
  const registry = createRegistry();
  registry.install(defineExtension({ name: 'mizu', tools }));

  const storage = await openNodeSqliteStorage(cfg.store);
  const context = BACKGROUND_CONTEXT;
  // cacheRetention: profile stream preference passed natively as
  // settings.stream.cacheRetention (HarnessSettings.stream is read live at
  // every resolution, so retuning keeps the same store). Unset omits the
  // field so the engine/provider default applies (validated above).
  const cacheRetention = cfg.cacheRetention;
  let settings = undefined;
  if (compaction !== undefined || cacheRetention !== undefined) {
    settings = {};
    if (compaction !== undefined) settings.compaction = compaction;
    if (cacheRetention !== undefined) settings.stream = { cacheRetention };
  }
  const harness = settings === undefined
    ? await Harness.open(storage, { models, registry }, context)
    : await Harness.open(storage, { models, registry, settings }, context);
  try {
    // Agent tools take the installed tool registrations (not bare names);
    // the registry holds the same objects installed above.
    const agent = { model: { provider: cfg.provider, modelId: cfg.model },
      tools, instructions: cfg.instructions, cwd: cfg.cwd };
    if (cfg.thinkingLevel) agent.thinkingLevel = cfg.thinkingLevel;
    const { root, submission: currentSubmission, usageBefore } = await submitWithCurrentAgent(harness, agent,
      { type: 'input', content: cfg.prompt, requestId: cfg.requestId }, context);
    submission = currentSubmission;
    const settled = await submission.wait(context);
    if (boundTripped) {
      const usage = await harness.usage(context).catch(() => null);
      const reason = drainTripped
        ? 'Drain bound refused the provider request'
        : 'Per-run provider request budget exhausted';
      return { durable_result: true, status: drainTripped ? 'drain' : 'request_bound', reason,
        finish_called: finishCalled, sealed, requests, observed: observations,
        usage: usageDelta(usageBefore, usage, `${cfg.provider}/${cfg.model}`),
        usage_models: Object.keys(usage?.models ?? {}), answer: null };
    }
    const usage = await harness.usage(context);
    const key = `${cfg.provider}/${cfg.model}`;
    let answer = null;
    if (settled.status === 'done' && settled.type === 'input' && settled.answer !== undefined) {
      try {
        const entry = await root.commit(tx => tx.entry(AssistantEntry, settled.answer), context);
        const text = JSON.stringify(entry?.model ?? entry ?? null);
        answer = text.length > 4096 ? text.slice(0, 4096) : text;
      } catch {
        answer = null; // transcript stays in storage; evidence stays bounded
      }
    }
    return { durable_result: true, ...settlementFields(settled),
      finish_called: finishCalled, sealed,
      requests, observed: observations,
      usage: usageDelta(usageBefore, usage, key),
      usage_models: Object.keys(usage?.models ?? {}), answer };
  } finally {
    await harness.close(context).catch(() => {});
  }
}
