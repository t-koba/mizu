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
const MAX_SETTLEMENT_DETAIL_CHARS = 4000;

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
  // Python owns absolute request/wall deadlines and bounded process cleanup.
  // A launcher-wide timer would cancel an admitted child during its join.
  try {
    process.stdout.write(JSON.stringify(await runOnce(cfg)) + '\n');
  } catch (error) {
    fail(error?.message || String(error));
  }
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
    const admittedSequence = ++sequence;
    try {
      await request(bridge, '_budget', { sequence: admittedSequence });
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
      return { durable_result: true, status: 'request_bound', reason: 'Per-run provider request budget exhausted',
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
