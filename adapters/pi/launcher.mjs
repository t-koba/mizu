/** Managed current SDK entry point. stdout belongs exclusively to runRpcMode. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Type } from '@earendil-works/pi-ai';
import { ModelRuntime, SettingsManager, SessionManager, createAgentSession,
  createAgentSessionRuntime, createAgentSessionServices, createCodemodeExtension,
  createToolSearchExtension, createMcpExtension, runRpcMode } from '@earendil-works/pi-coding-agent';
import { register } from './register.mjs';
import { meterRuntime } from './model-runtime.mjs';

if (process.argv.includes('--check-contract')) {
  const checked = { 'ModelRuntime': ModelRuntime.create, 'createAgentSession': createAgentSession,
    'createAgentSessionRuntime': createAgentSessionRuntime, 'runRpcMode': runRpcMode,
    'createMcpExtension': createMcpExtension };
  for (const fn of Object.values(checked)) {
    if (typeof fn !== 'function') throw new Error('Required SDK capability missing');
  }
  process.stdout.write(JSON.stringify({ contract: 'managed-pi-sdk', exports: Object.keys(checked),
    inference: 'not_run' })+'\n');
} else {
  const cfg = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  const bridge = JSON.parse(readFileSync(process.env.MIZU_BRIDGE_CONFIG, 'utf8'));
  bridge.engine_tools = cfg.engine_tools;
  const modelRuntime = await ModelRuntime.create({ authPath: join(cfg.agentDir, 'auth.json'),
    modelsPath: join(cfg.agentDir, 'models.json'), modelsStorePath: join(cfg.agentDir, 'models-cache.json'),
    allowModelNetwork: false });
  const meter = meterRuntime(modelRuntime, bridge);
  if (cfg.resources.some(r => !['extension','skill','prompt','theme'].includes(r.kind))) throw new Error('Unsupported Pi resource kind');
  const factories = [{ name: 'mizu', factory: pi => register(pi, Type, bridge) }];
  if (cfg.options.codemode) factories.push({ name: 'codemode', factory: createCodemodeExtension(), builtin: true });
  if (cfg.options.toolSearch) factories.push({ name: 'tool-search', factory: createToolSearchExtension(), builtin: true });
  if (Object.keys(cfg.mcp_servers).length) factories.push({ name: 'mcp', builtin: true,
    factory: createMcpExtension({ loadConfig: () => ({ servers: Object.entries(cfg.mcp_servers).map(([name, config]) =>
      ({ name, config, source: 'mizu-profile', scope: 'extension' })), errors: [], autoEnableCodemode: Boolean(cfg.options.codemode) }) }) });
  const manager = cfg.resume ? SessionManager.open(cfg.resume, cfg.sessionDir, cfg.cwd) : SessionManager.create(cfg.cwd, cfg.sessionDir);
  const runtime = await createAgentSessionRuntime(async ({ cwd, agentDir, sessionManager }) => {
    const settingsManager = SettingsManager.inMemory(cfg.options.settings ?? {});
    const paths = kind => cfg.resources.filter(r => r.kind === kind).map(r => r.path);
    const services = await createAgentSessionServices({ cwd, agentDir, settingsManager, modelRuntime, resourceLoaderOptions: {
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      additionalExtensionPaths: paths('extension'), additionalSkillPaths: paths('skill'),
      additionalPromptTemplatePaths: paths('prompt'), additionalThemePaths: paths('theme'),
      extensionFactories: factories, systemPrompt: cfg.systemPrompt } });
    const extensionErrors = services.resourceLoader.getExtensions().errors;
    services.diagnostics.push(...services.resourceLoader.getSkills().diagnostics,
      ...services.resourceLoader.getPrompts().diagnostics, ...services.resourceLoader.getThemes().diagnostics);
    if (extensionErrors.length || services.diagnostics.some(item => item.type === 'error')) {
      throw new Error(JSON.stringify({ message: 'Pi resource/provider setup failed', extensionErrors, diagnostics: services.diagnostics }));
    }
    const resourceLoader = services.resourceLoader;
    const model = modelRuntime.getModel(cfg.provider, cfg.model);
    if (!model) throw new Error('Configured exact model is unavailable');
    const result = await createAgentSession({ cwd, agentDir, modelRuntime, model,
      thinkingLevel: cfg.options.thinkingLevel, excludeTools: cfg.options.excludeTools, scopedModels: cfg.options.scopedModels, settingsManager, sessionManager, resourceLoader,
      noTools: 'builtin', tools: [...bridge.tools.map(t => t.name), ...cfg.engine_tools] });
    if (result.modelFallbackMessage) throw new Error('Session resume changed the selected model');
    result.session.subscribe(event => {
      if (event.type === 'agent_settled') meter.flush().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
    });
    return { ...result, services, diagnostics: services.diagnostics };
  }, { cwd: cfg.cwd, agentDir: cfg.agentDir, sessionManager: manager });
  await runRpcMode(runtime);
}
