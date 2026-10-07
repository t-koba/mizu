/** Managed pi-durable entry point: real durable Harness over file-backed SQLite.
 *
 * One-shot protocol: `node launcher.mjs <effective.json>` runs one admitted
 * submission to settlement and prints a single JSON result document to
 * stdout (stderr carries progress only). No RPC, no JSONL event stream.
 *
 * Durable contracts (from @earendil-works/pi-durable 1.0.4):
 * - openNodeSqliteStorage(file): WAL SQLite; commits survive process crashes.
 * - Harness.open(storage, {models, registry}): one Session line of commits.
 * - root.submit({type:'input', content, requestId}): retried requestId
 *   returns the existing submission instead of submitting twice.
 * - submission.wait(): resolves done/unanswered. Interrupted tool calls are
 *   never rerun (default replay unsafe): the model gets an `interrupted`
 *   result, matching the Python driver's unknown-completion rule.
 * - harness.resume(): continue work a dead process left unfinished.
 * - harness.usage(): per provider/model + per tool spend.
 *
 * Authority preserved from the pi engine: exact provider/model pin (built-in
 * providers only; custom models.json providers refuse loudly), bridge tools
 * only (native host tools refused Python-side), per-request budget admission
 * and usage reporting around every provider call, mizu_finish seal required.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Type } from '@earendil-works/pi-ai';
import { builtinModels } from '@earendil-works/pi-ai/providers/all';
import { BACKGROUND_CONTEXT } from '@earendil-works/chord/context';
import { AssistantEntry, createRegistry, defineExtension, defineTool, Harness } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { request, typeSchema } from './bridge-client.mjs';

const CONTRACT_EXPORTS = ['Harness', 'createRegistry', 'defineExtension', 'defineTool', 'openNodeSqliteStorage', 'builtinModels'];

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
      const source = (async () => { await hooks.admit(); return original(...args); })();
      const pending = source.then(stream => stream.result()).then(async value => {
        await hooks.report(value?.usage, value);
        return value;
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
      await hooks.admit();
      const result = await original(...args);
      await hooks.report(result?.usage, result);
      return result;
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
    ['openNodeSqliteStorage', openNodeSqliteStorage], ['builtinModels', builtinModels],
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
  const watchdog = setTimeout(() => fail('Launcher watchdog deadline exceeded'), Number(cfg.deadline_ms) || 600000);
  watchdog.unref?.();
  try {
    process.stdout.write(JSON.stringify(await runOnce(cfg)) + '\n');
  } catch (error) {
    fail(error?.message || String(error));
  } finally {
    clearTimeout(watchdog);
  }
}

async function runOnce(cfg) {
  for (const field of ['provider', 'model', 'instructions', 'store', 'requestId', 'prompt', 'cwd']) {
    if (typeof cfg[field] !== 'string' || !cfg[field]) throw new Error(`Effective configuration lacks ${field}`);
  }
  if (!Array.isArray(cfg.tools) || !cfg.tools.length) throw new Error('Effective configuration carries no bridge tools');
  if (!Number.isInteger(cfg.max_turns) || cfg.max_turns < 1) throw new Error('Effective configuration lacks max_turns');
  const maxTurns = cfg.max_turns;
  if (cfg.engine_tools?.length) throw new Error('Native host tools bypass the OCI bridge');
  if (cfg.resources?.length) throw new Error('Resource kinds are unsupported on pi-durable');
  if (cfg.mcp_servers && Object.keys(cfg.mcp_servers).length) throw new Error('MCP servers are unsupported on pi-durable');
  const bridge = JSON.parse(readFileSync(process.env.MIZU_BRIDGE_CONFIG, 'utf8'));

  // Exact-model pin over built-in providers only. Custom models.json
  // providers stay on pi; here they refuse loudly instead of guessing.
  const models = builtinModels();
  const model = models.getModel(cfg.provider, cfg.model);
  if (!model) throw new Error(`Configured exact model is unavailable on pi-durable: ${cfg.provider}/${cfg.model}`);
  const auth = await models.checkAuth(cfg.provider).catch(() => undefined);
  if (!auth) throw new Error(`Provider auth is not configured for ${cfg.provider}`);

  // Admission + usage metering around every provider call, mirroring the
  // pi engine: one _budget reservation before dispatch, one _model_usage
  // report after. complete* delegate to stream*, so wrapping the three
  // stream entry points meters each provider request exactly once.
  let sequence = 0;
  let requests = 0;
  let boundTripped = false;
  let submission = null;
  const observations = [];
  async function admit() {
    await request(bridge, '_budget', { sequence: ++sequence });
    requests++;
    if (requests > maxTurns && !boundTripped) {
      // The grant's remaining turn budget is spent: stop Harness work
      // instead of running unbounded inside one submission. wait() below
      // settles the aborted submission; the flag maps it to turn_bound.
      boundTripped = true;
      try { if (submission) await submission.abort(context); } catch {}
      const error = new Error('Durable turn bound exhausted');
      error.code = 'TURN_BOUND';
      throw error;
    }
  }
  async function reportUsage(usage, shape) {
    if (!usage) return;
    observations.push({ provider: shape?.provider ?? cfg.provider, model: shape?.model ?? cfg.model,
      thinkingLevel: shape?.thinkingLevel ?? cfg.thinkingLevel ?? null });
    await request(bridge, '_model_usage', { sequence: ++sequence, usage,
      model: { provider: cfg.provider, model: cfg.model, thinkingLevel: cfg.thinkingLevel ?? null } });
  }
  meterModels(models, { admit, report: reportUsage });

  // Bridge tools as one durable extension. Intent commits before execute;
  // interrupted executions are never rerun (replay unsafe default), so an
  // uncertain side effect surfaces as `interrupted`, never a blind repeat.
  let finishCalled = false;
  let sealed = null;
  const tools = cfg.tools.map(tool => defineTool({
    name: tool.name,
    description: tool.description,
    parameters: typeSchema(Type, tool.inputSchema),
    execute: async (args) => {
      const result = await request(bridge, tool.operation, args);
      if (tool.operation === 'finish') {
        finishCalled = true;
        sealed = result;
        return { content: [{ type: 'text', text: JSON.stringify(result) }], details: result,
          control: { terminate: true } };
      }
      return { content: [{ type: 'text', text: JSON.stringify(result) }], details: result };
    },
  }));
  const registry = createRegistry();
  registry.install(defineExtension({ name: 'mizu', tools }));

  const storage = await openNodeSqliteStorage(cfg.store);
  const context = BACKGROUND_CONTEXT;
  const harness = await Harness.open(storage, { models, registry }, context);
  try {
    harness.resume(); // continue work a dead process left unfinished
    // Agent tools take the installed tool registrations (not bare names);
    // the registry holds the same objects installed above.
    const agent = { model: { provider: cfg.provider, modelId: cfg.model },
      tools, instructions: cfg.instructions, cwd: cfg.cwd };
    if (cfg.thinkingLevel) agent.thinkingLevel = cfg.thinkingLevel;
    const root = await harness.root(context, { agent });
    const resolved = await root.agent(context);
    if (resolved?.model?.provider !== cfg.provider || resolved?.model?.modelId !== cfg.model) {
      throw new Error('Durable session resolved a different model/provider');
    }
    submission = await root.submit({ type: 'input', content: cfg.prompt, requestId: cfg.requestId }, context);
    const settled = await submission.wait(context);
    if (boundTripped) {
      const usage = await harness.usage(context).catch(() => null);
      return { durable_result: true, status: 'turn_bound', reason: 'Durable turn bound exhausted',
        finish_called: finishCalled, sealed, requests, observed: observations,
        usage: usage?.models?.[`${cfg.provider}/${cfg.model}`] ?? null,
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
    return { durable_result: true, status: settled.status,
      reason: settled.status === 'done' ? null : (settled.reason ?? 'unanswered'),
      finish_called: finishCalled, sealed,
      requests, observed: observations,
      usage: usage?.models?.[key] ?? null,
      usage_models: Object.keys(usage?.models ?? {}), answer };
  } finally {
    await harness.close(context).catch(() => {});
  }
}
