/** Reserve public runtime operations before dispatch, independent of provider hooks. */
import { request } from './bridge-client.mjs';
const reservation = Symbol('mizu-runtime-request');

export function meterRuntime(runtime, config, call = request) {
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
        if (!admitted) {
          const candidate = ++sequence;
          await call(config, '_budget', { sequence: candidate });
          admittedSequence = candidate;
        }
        return original(model, context, { ...options, [reservation]: model });
      })();
      const result = track(source.then(stream => stream.result()).then(async value => { await record(value); return value; })
        .finally(async () => {
          if (admittedSequence !== undefined) await call(config, '_request_done', { sequence: admittedSequence });
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
      const admittedSequence = ++sequence;
      await call(config, '_budget', { sequence: admittedSequence });
      try {
        const result = await original(model, context, options);
        await record(result);
        return result;
      } finally {
        await call(config, '_request_done', { sequence: admittedSequence });
      }
    };
  }
  return { flush: async () => { await Promise.all([...pending]); } };
}
