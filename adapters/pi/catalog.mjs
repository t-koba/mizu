/** No-inference inspection of a registered provider, never the composed SDK catalog.
 * ModelRuntime's store is shared with overlays and has no source-bound success or
 * completeness metadata. Do not present its timestamps as native observations.
 */
import { join } from 'node:path';
import { ModelRuntime, SettingsManager, createAgentSessionServices } from '@earendil-works/pi-coding-agent';

export function catalogError(error) {
  // Codes only: exception messages, headers and URLs may contain credentials.
  if (['ENOTFOUND', 'EAI_AGAIN', 'ECONNREFUSED', 'ENETUNREACH'].includes(error?.code)) return 'offline';
  if (['auth', 'oauth'].includes(error?.code)) return 'auth_failed';
  return 'refresh_failed';
}

export async function inspectNativeCatalog(runtime, cfg) {
  const report = { engine: 'pi', provider: cfg.provider, selected_model: cfg.model,
    inference: 'not_run', source: 'unsupported', status: 'unsupported', models: [],
    refresh: cfg.refresh ? 'unsupported' : 'not_requested', error: null,
    last_successful_observation: null, freshness: 'unknown', completeness: 'unknown' };
  const provider = runtime.getRegisteredNativeProvider?.(cfg.provider);
  if (!provider || provider.id !== cfg.provider || typeof provider.getModels !== 'function'
      || typeof provider.refreshModels !== 'function') return report;
  report.source = 'registered_native_provider';
  report.status = 'available';
  try {
    // A cache-only refresh also reports restoration/auth errors. No global
    // network refresh: a built-in wrapper can otherwise fetch the pi.dev overlay.
    const result = await runtime.refresh({ providers: [cfg.provider],
      allowNetwork: cfg.refresh === true, force: cfg.refresh === true });
    const error = result.errors.get(cfg.provider);
    if (error || result.aborted) {
      report.error = result.aborted ? 'aborted' : catalogError(error);
      report.status = 'error';
    }
    if (cfg.refresh) report.refresh = report.error ? 'failed' : 'completed';
  } catch (error) {
    report.error = catalogError(error);
    report.status = 'error';
    if (cfg.refresh) report.refresh = 'failed';
  }
  try {
    const models = provider.getModels();
    // Only expose bounded identifiers, not arbitrary metadata/headers/base URLs.
    if (!Array.isArray(models) || models.length > 4096) throw new Error();
    const ids = new Set();
    const rows = models.map(model => {
      if (model.provider !== cfg.provider || typeof model.id !== 'string'
          || !model.id || model.id.length > 512 || /[\x00-\x1f\x7f]/.test(model.id)
          || ids.has(model.id)) throw new Error();
      ids.add(model.id);
      const resolved = runtime.getModel(cfg.provider, model.id);
      return { id: model.id, resolvable: Boolean(resolved && model.api && resolved.api === model.api
        && Number.isFinite(model.contextWindow) && model.contextWindow > 0
        && Number.isFinite(model.maxTokens) && model.maxTokens > 0
        && Number.isFinite(resolved.contextWindow) && resolved.contextWindow > 0
        && Number.isFinite(resolved.maxTokens) && resolved.maxTokens > 0) };
    });
    if (Buffer.byteLength(JSON.stringify(rows), 'utf8') > 768 * 1024) throw new Error();
    report.models = rows;
    if (!report.error && rows.some(model => !model.resolvable)) report.status = 'incomplete';
  } catch {
    report.models = [];
    report.status = 'unsupported';
    report.error = 'invalid_native_listing';
  }
  return report;
}

export async function catalog(cfg) {
  const runtime = await ModelRuntime.create({ authPath: join(cfg.agentDir, 'auth.json'),
    modelsPath: join(cfg.agentDir, 'models.json'), modelsStorePath: join(cfg.agentDir, 'models-cache.json'),
    allowModelNetwork: false });
  // Same reviewed extension loading/registration as ordinary Pi runs, without
  // creating a session, bridge, MCP server or making a model request.
  const services = await createAgentSessionServices({ cwd: cfg.cwd, agentDir: cfg.agentDir,
    modelRuntime: runtime, settingsManager: SettingsManager.inMemory(cfg.options.settings ?? {}),
    resourceLoaderOptions: { noExtensions: true, noSkills: true, noPromptTemplates: true,
      noThemes: true, noContextFiles: true,
      additionalExtensionPaths: cfg.resources.filter(r => r.kind === 'extension').map(r => r.path) } });
  if (services.resourceLoader.getExtensions().errors.length
      || services.diagnostics.some(item => item.type === 'error')) throw new Error();
  return inspectNativeCatalog(runtime, cfg);
}
