/** Reserve public runtime operations before dispatch, independent of provider hooks. */
import { request } from './bridge-client.mjs';
const reservation = Symbol('mizu-runtime-request');
// Matches the pinned SDK's DEFAULT_THINKING_LEVEL; the SDK-backed contract
// test compares this fallback to the installed SDK so upgrades cannot drift silently.
const DEFAULT_THINKING_LEVEL = 'medium';
// Keep this validation local so bridge/runtime tests do not require the SDK.
// The installed-SDK contract suite checks the list against MODEL_THINKING_LEVELS.
const THINKING_LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);

export function resolveThinkingLevel(selection, model) {
  const options = selection?.options ?? {};
  if (typeof options !== 'object' || Array.isArray(options)) throw new Error('Invalid Pi profile options');
  const settings = options.settings ?? {};
  if (typeof settings !== 'object' || Array.isArray(settings)) throw new Error('Invalid Pi profile settings');
  const modelLevels = settings.modelThinkingLevels ?? {};
  if (typeof modelLevels !== 'object' || Array.isArray(modelLevels)) throw new Error('Invalid Pi model thinking settings');
  const modelKey = `${model.provider}/${model.id}`;
  let level = options.thinkingLevel;
  if (level === undefined && Object.hasOwn(modelLevels, modelKey)) level = modelLevels[modelKey];
  if (level === undefined) level = settings.defaultThinkingLevel;
  if (level === undefined) level = DEFAULT_THINKING_LEVEL;
  if (!THINKING_LEVELS.has(level)) throw new Error('Invalid selected Pi thinking level');
  return level;
}

export function meterRuntime(runtime, config, call = request, authorize = () => {}) {
  let sequence = 0;
  const seen = new WeakSet();
  const pending = new Set();
  async function record(result) {
    if (!result || typeof result !== 'object' || seen.has(result)) return;
    seen.add(result);
    if (!result.usage) return;
    await call(config, '_model_usage', { sequence: ++sequence, usage: result.usage,
      model: { provider: result.provider ?? null, model: result.model ?? null, thinkingLevel: result.thinkingLevel ?? null } });
  }
  function track(promise) {
    pending.add(promise);
    promise.finally(() => pending.delete(promise)).catch(() => {});
    return promise;
  }
  for (const name of ['stream', 'streamSimple', 'streamDeferred']) {
    if (!runtime[name]) continue;
    const original = runtime[name].bind(runtime);
    runtime[name] = (model, context, options = {}) => {
      // Virtual routing itself makes no provider request; its auxiliary calls
      // and recursively dispatched physical request use this same runtime.
      const admitted = model.api === 'pi-virtual' || options[reservation] === model;
      let admittedSequence;
      const source = (async () => {
        authorize(model);
        if (!admitted) {
          const candidate = ++sequence;
          await call(config, '_budget', { sequence: candidate });
          admittedSequence = candidate;
        }
        return original(model, context, { ...options, [reservation]: model });
      })();
      const result = track(source.then(stream => stream.result()).then(async value => { await record(value); return value; })
        .then(async value => {
          if (admittedSequence !== undefined) await call(config, '_request_done', { sequence: admittedSequence, failed: false });
          return value;
        }, async error => {
          if (admittedSequence !== undefined) await call(config, '_request_done', { sequence: admittedSequence, failed: true });
          throw error;
        }));
      return {
        result: () => result,
        async *[Symbol.asyncIterator]() {
          yield* await source;
          await result;
        },
      };
    };
  }
  for (const name of ['classify', 'generateImages']) {
    const original = runtime[name].bind(runtime);
    runtime[name] = async (model, context, options) => {
      authorize(model);
      const admittedSequence = ++sequence;
      await call(config, '_budget', { sequence: admittedSequence });
      let failed = true;
      try {
        const result = await original(model, context, options);
        await record(result);
        failed = false;
        return result;
      } finally {
        await call(config, '_request_done', { sequence: admittedSequence, failed });
      }
    };
  }
  return { flush: async () => { await Promise.all([...pending]); } };
}

/** Host-authorized upgrades at the SDK's completed-result boundary, never events. */

export function modelAuthority() {
  let authorized;
  let failed = false;
  return {
    allow(model) { authorized = model; },
    fail() { failed = true; },
    check(model) {
      if (failed || !authorized || model.provider !== authorized.provider || model.id !== authorized.id) {
        throw new Error('Pi model is not host-authorized');
      }
    },
  };
}

export async function installReselection(session, runtime, config, bridge, authority, call = request) {
  async function apply(selection) {
    const model = runtime.getModel(selection.provider, selection.model);
    if (!model) throw new Error('Host-authorized Pi model unavailable');
    if (model.api === 'pi-virtual') throw new Error('Pi virtual models are outside the re-selection authority boundary');
    const prior = session.sessionManager.getBranch().findLast(entry => entry.type === 'model_change');
    if (session.model?.provider !== model.provider || session.model?.id !== model.id ||
        prior?.provider !== model.provider || prior?.modelId !== model.id) await session.setModel(model);
    // Session history belongs to the family, not this selected profile. Resolve
    // omitted settings from the selected profile instead of inheriting the
    // previous candidate's thinking_level_change entry.
    session.setThinkingLevel(resolveThinkingLevel(selection, model));
    if (selection.options?.cacheRetention === 'long') process.env.PI_CACHE_RETENTION = 'long';
    else delete process.env.PI_CACHE_RETENTION;
    verify(model);
    return model;
  }
  function verify(model) {
    const entry = session.sessionManager.getBranch().findLast(entry => entry.type === 'model_change');
    if (session.model?.provider !== model.provider || session.model?.id !== model.id ||
        entry?.provider !== model.provider || entry?.modelId !== model.id) {
      throw new Error('Pi model application/resume state mismatch');
    }
  }
  const previous = session.agent.prepareNextTurnWithContext;
  if (typeof previous !== 'function') throw new Error('Pi completed-result boundary unavailable');
  // Reconcile the entry model with the retained transcript before any request.
  // A real change gets one model_change entry; unchanged resumes add none.
  try { authority.allow(await apply(config)); }
  catch (error) { authority.fail(); throw error; }
  session.agent.prepareNextTurnWithContext = async (turn, signal) => {
    try {
      const snapshot = await previous.call(session.agent, turn, signal);
      const response = await call(bridge, '_pi_reselect', {}, signal);
      if (!response || !Object.hasOwn(response, 'switch')) throw new Error('Invalid Pi authorization');
      if (response.switch !== null) {
        const model = await apply(response.switch);
        const ack = await call(bridge, '_pi_reselect_applied', {
          provider: model.provider, model: model.id, authorization: response.switch.authorization,
        }, signal);
        if (ack?.applied !== true) throw new Error('Pi authorization commit failed');
        authority.allow(model);
      }
      authority.check(session.model);
      verify(session.model);
      return { ...snapshot, model: session.model, thinkingLevel: session.thinkingLevel };
    } catch (error) {
      authority.fail();
      throw error;
    }
  };
}
