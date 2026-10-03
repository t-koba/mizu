/** Current Pi tool registration. Authority is independent of exposure. */
import { request, typeSchema } from './bridge-client.mjs';

export function register(pi, Type, config, dependencies = {}) {
  const call = dependencies.request || request;
  for (const tool of config.tools) {
    pi.registerTool({
      name: tool.name, label: tool.name, description: tool.description,
      parameters: typeSchema(Type, tool.inputSchema),
      outputSchema: Type.Object({}, { additionalProperties: true }),
      exposure: tool.operation === 'finish' ? 'model-only' : 'direct',
      executionMode: 'sequential',
      async execute(_id, args, signal) {
        const result = await call(config, tool.operation, args, signal);
        return { content: [{ type: 'text', text: JSON.stringify(result) }],
          structuredContent: result, details: { operation: tool.operation },
          ...(tool.operation === 'finish' ? { terminate: true } : {}) };
      },
    });
  }
  pi.on('tool_call', async event => {
    if (config.tools.some(tool => tool.name === event.toolName)) return;
    try { await call(config, '_engine_tool', { name: event.toolName }); }
    catch (error) { return { block: true, reason: String(error) }; }
  });
  pi.on('session_start', async () => {
    await call(config, '_hello', {});
  });
}
