/** Managed pi-durable entry point: real durable Harness over file-backed SQLite.
 *
 * One-shot protocol: `node launcher.mjs <effective.json>` runs one admitted
 * submission to settlement and prints a single JSON result document to
 * stdout (stderr carries progress only). No RPC, no JSONL event stream.
 *
 * Durable contracts (from @earendil-works/pi-durable 1.1.0):
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
 * - harness.usage(): per provider/model + per tool spend.
 *
 * Authority preserved from the pi engine: exact provider/model pin (built-in
 * providers only; custom models.json providers refuse loudly), bridge tools
 * only (native host tools refused Python-side), per-request budget admission
 * and usage reporting around every provider call, mizu_finish seal required.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import lockfile from 'proper-lockfile';
import { Type } from '@earendil-works/pi-ai';
import { builtinModels } from '@earendil-works/pi-ai/providers/all';
import { BACKGROUND_CONTEXT } from '@earendil-works/chord/context';
import { AssistantEntry, createRegistry, defineExtension, defineTool, Harness } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { request, typeSchema } from './bridge-client.mjs';

const CONTRACT_EXPORTS = ['Harness', 'createRegistry', 'defineExtension', 'defineTool', 'openNodeSqliteStorage', 'builtinModels'];

/**
 * File-backed provider credentials over Pi's login store (auth.json).
 *
 * The store is the pi engine's `AuthStorage`/`FileAuthStorageBackend`
 * protocol in miniature: `auth.json` read-modify-write serialized by
 * `proper-lockfile` with the same options Pi uses (`realpath: false`,
 * `stale: 30000`), file mode 0600 and directory mode 0700. `pi-ai`
 * runs OAuth refresh inside `modify`, so the rotated refresh token is
 * persisted before the lock is released and concurrent pi / pi-durable
 * processes refresh exactly once instead of spending the same rotating
 * token twice (observed 2026-10-11: in-memory seed discarded the
 * replacement, then `invalid_grant` everywhere until re-login).
 *
 * Reads on a missing file stay env-only without creating it; `modify`
 * ensures the directory/file. Entry shapes mirror Pi's auth.json
 * validation (api_key key/env, oauth access/refresh/expires); anything
 * else fails closed. Any other read or shape failure refuses loudly
 * instead of running unauthenticated.
 */
const AUTH_FILE_WRITE_OPTIONS = { encoding: 'utf-8', mode: 0o600 };

function validateCredential(providerId, credential) {
  if (typeof credential !== 'object' || credential === null || Array.isArray(credential)) {
    throw new Error(`Invalid Pi auth credential for provider "${providerId}"`);
  }
  if (credential.type === 'api_key') {
    const validKey = credential.key === undefined || typeof credential.key === 'string';
    const validEnv = credential.env === undefined
      || (typeof credential.env === 'object' && credential.env !== null && !Array.isArray(credential.env)
        && Object.values(credential.env).every(entry => typeof entry === 'string'));
    if (!validKey || !validEnv) throw new Error(`Invalid Pi auth credential for provider "${providerId}"`);
  } else if (credential.type === 'oauth') {
    if (typeof credential.access !== 'string' || typeof credential.refresh !== 'string'
      || typeof credential.expires !== 'number' || !Number.isFinite(credential.expires)) {
      throw new Error(`Invalid Pi auth credential for provider "${providerId}"`);
    }
  } else {
    throw new Error(`Invalid Pi auth credential for provider "${providerId}"`);
  }
}

function parseAuthData(content) {
  if (!content) return {};
  let parsed;
  try {
    parsed = JSON.parse(content);
  } catch (error) {
    throw new Error(`Invalid Pi auth store: ${error.message}`);
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('Invalid Pi auth store: expected an object');
  }
  for (const [providerId, credential] of Object.entries(parsed)) validateCredential(providerId, credential);
  return parsed;
}

function ensureParentDir(authPath) {
  const dir = dirname(authPath);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true, mode: 0o700 });
}

function ensureFileExists(authPath) {
  if (!existsSync(authPath)) writeFileSync(authPath, '{}', AUTH_FILE_WRITE_OPTIONS);
}

async function acquireLockAsync(authPath, signal, onCompromised) {
  const staleMs = 30000;
  const maxDelayMs = 2000;
  const deadline = Date.now() + staleMs;
  let retry = 0;
  for (;;) {
    signal?.throwIfAborted();
    let release;
    try {
      release = await lockfile.lock(authPath, { realpath: false, retries: 0, stale: staleMs, onCompromised });
    } catch (error) {
      signal?.throwIfAborted();
      const code = typeof error === 'object' && error !== null && 'code' in error ? String(error.code) : undefined;
      const remainingMs = deadline - Date.now();
      if (code !== 'ELOCKED' || remainingMs <= 0) throw error;
      const baseDelayMs = Math.min(10 * 2 ** retry, maxDelayMs / 2);
      retry++;
      const delayMs = Math.min(Math.round(baseDelayMs * (1 + Math.random())), remainingMs);
      if (signal) await sleep(delayMs, undefined, { signal });
      else await sleep(delayMs);
      continue;
    }
    if (signal?.aborted) {
      await release();
      signal.throwIfAborted();
    }
    return release;
  }
}

async function withAuthLockAsync(authPath, fn, options) {
  options?.signal?.throwIfAborted();
  ensureParentDir(authPath);
  ensureFileExists(authPath);
  let release;
  let compromised = false;
  let compromisedError;
  try {
    release = await acquireLockAsync(authPath, options?.signal, (error) => {
      compromised = true;
      compromisedError = error;
    });
    if (compromised) throw compromisedError ?? new Error('Auth storage lock was compromised');
    options?.signal?.throwIfAborted();
    const current = existsSync(authPath) ? readFileSync(authPath, 'utf-8') : undefined;
    const { result, next } = await fn(current);
    if (compromised) throw compromisedError ?? new Error('Auth storage lock was compromised');
    options?.signal?.throwIfAborted();
    if (next !== undefined) writeFileSync(authPath, next, AUTH_FILE_WRITE_OPTIONS);
    if (compromised) throw compromisedError ?? new Error('Auth storage lock was compromised');
    return result;
  } finally {
    if (release) {
      try {
        await release();
      } catch {
        // Ignore unlock errors when the lock was compromised.
      }
    }
  }
}

export class FileCredentialStore {
  constructor(authPath) {
    if (typeof authPath !== 'string' || !authPath) throw new Error('Credential store requires authPath');
    this.authPath = authPath;
  }

  async read(providerId, options) {
    options?.signal?.throwIfAborted();
    let raw;
    try {
      raw = readFileSync(this.authPath, 'utf8');
    } catch (error) {
      if (error?.code === 'ENOENT') return undefined;
      throw new Error(`Cannot read Pi auth store: ${error.message}`);
    }
    options?.signal?.throwIfAborted();
    // Serialized read: concurrent refresh persistence cannot tear the file.
    return withAuthLockAsync(this.authPath, async (current) => {
      const data = parseAuthData(current ?? raw);
      return { result: data[providerId] === undefined ? undefined : structuredClone(data[providerId]) };
    }, options);
  }

  async list(options) {
    options?.signal?.throwIfAborted();
    let raw;
    try {
      raw = readFileSync(this.authPath, 'utf8');
    } catch (error) {
      if (error?.code === 'ENOENT') return [];
      throw new Error(`Cannot read Pi auth store: ${error.message}`);
    }
    return withAuthLockAsync(this.authPath, async (current) => {
      const data = parseAuthData(current ?? raw);
      return { result: Object.entries(data).map(([providerId, credential]) => ({ providerId, type: credential.type })) };
    }, options);
  }

  async modify(providerId, fn, options) {
    const result = await withAuthLockAsync(this.authPath, async (current) => {
      const data = parseAuthData(current);
      const next = await fn(data[providerId] === undefined ? undefined : structuredClone(data[providerId]));
      if (next === undefined) return { result: data[providerId] };
      validateCredential(providerId, next);
      const merged = { ...data, [providerId]: next };
      return { result: next, next: JSON.stringify(merged, null, 2) };
    }, options);
    return result === undefined ? undefined : structuredClone(result);
  }

  async delete(providerId, options) {
    await withAuthLockAsync(this.authPath, async (current) => {
      const data = parseAuthData(current);
      delete data[providerId];
      return { result: undefined, next: JSON.stringify(data, null, 2) };
    }, options);
  }
}

/**
 * Open the shared Pi login store for `builtinModels({ credentials })`.
 * A missing file keeps env-only behavior: reads resolve absent without
 * creating the file, and refresh persistence (`modify`) is the only
 * write path. Malformed content refuses loudly.
 */
export async function credentialsFromFile(authPath) {
  const store = new FileCredentialStore(authPath);
  try {
    await store.list();
  } catch (error) {
    if (error?.code === 'ENOENT') return store;
    throw error;
  }
  return store;
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
  if (cfg.engine_tools?.length) throw new Error('Native host tools bypass the OCI bridge');
  if (cfg.resources?.length) throw new Error('Resource kinds are unsupported on pi-durable');
  if (cfg.mcp_servers && Object.keys(cfg.mcp_servers).length) throw new Error('MCP servers are unsupported on pi-durable');
  const bridge = JSON.parse(readFileSync(process.env.MIZU_BRIDGE_CONFIG, 'utf8'));

  // Exact-model pin over built-in providers only. Custom models.json
  // providers stay on pi; here they refuse loudly instead of guessing.
  // Credentials resolve like pi: the shared engine-directory login store
  // (authPath) plus environment API keys. The file-backed store persists
  // OAuth refreshes under Pi's cross-process lock, so a rotating refresh
  // token is never spent without write-back.
  if (typeof cfg.authPath !== 'string' || !cfg.authPath) throw new Error('Effective configuration lacks authPath');
  const models = builtinModels({ credentials: await credentialsFromFile(cfg.authPath) });
  const model = models.getModel(cfg.provider, cfg.model);
  if (!model) throw new Error(`Configured exact model is unavailable on pi-durable: ${cfg.provider}/${cfg.model}`);
  const auth = await models.checkAuth(cfg.provider).catch(() => undefined);
  if (!auth) {
    throw new Error(`Provider auth is not configured for ${cfg.provider}`);
  }

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
    try {
      await request(bridge, '_budget', { sequence: ++sequence });
    } catch (error) {
      // The single per-unit spend bound (requests_per_run) is reached:
      // stop Harness work instead of running unbounded inside one
      // submission. wait() below settles the aborted submission; the flag
      // maps it to request_bound. Any other admission failure keeps its
      // own path (a daily-budget refusal still defers via admission_wait).
      if (!boundTripped && error && typeof error.message === 'string'
          && error.message.includes('Per-run provider request budget exhausted')) {
        boundTripped = true;
        try { if (submission) await submission.abort(context); } catch {}
      }
      throw error;
    }
    requests++;
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
  const harness = compaction === undefined
    ? await Harness.open(storage, { models, registry }, context)
    : await Harness.open(storage, { models, registry, settings: { compaction } }, context);
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
      return { durable_result: true, status: 'request_bound', reason: 'Per-run provider request budget exhausted',
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
