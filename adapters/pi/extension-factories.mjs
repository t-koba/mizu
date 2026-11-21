/** Shared extension composition and tool selection for the managed Pi launcher. SDK-agnostic. */
export function extensionFactories(cfg, bridge, sdk, mcpOptions = {}) {
  const { register, createCodemodeExtension, createToolSearchExtension, createMcpExtension } = sdk;
  const factories = [{ name: 'mizu', factory: pi => register(pi, bridge,
    { requireAuthority: true, authoritySections: cfg.authority_sections, systemCwd: cfg.promptCwd }) }];
  if (cfg.options.codemode) factories.push({ name: 'codemode', factory: createCodemodeExtension(), builtin: true });
  if (cfg.options.toolSearch) factories.push({ name: 'tool-search', factory: createToolSearchExtension(), builtin: true });
  if (Object.keys(cfg.mcp_servers).length) factories.push({ name: 'mcp', builtin: true,
    factory: createMcpExtension({ loadConfig: () => ({ servers: Object.entries(cfg.mcp_servers).map(([name, config]) =>
      ({ name, config, source: 'mizu-profile', scope: 'extension' })), errors: [], autoEnableCodemode: Boolean(cfg.options.codemode) }),
      ...mcpOptions }) });
  return factories;
}

export function sessionToolSelection(cfg, bridge) {
  return { thinkingLevel: cfg.options.thinkingLevel, excludeTools: cfg.options.excludeTools,
    scopedModels: cfg.options.scopedModels, noTools: 'builtin',
    tools: [...bridge.tools.map(t => t.name), ...cfg.engine_tools] };
}
