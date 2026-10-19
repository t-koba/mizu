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
    // ToolCallEvent.toolCallId is SDK event metadata (including nested parent
    // ids), separate from event.input. Missing ids fail closed
    // in Context; never fall back to model-supplied arguments or parent ids.
    try {
      const result = await call(config, '_engine_tool', {
        name: event.toolName, invocation_id: event.toolCallId,
      });
      if (result?.allowed !== true) return { block: true, reason: 'Tool admission refused' };
    }
    catch (error) { return { block: true, reason: String(error) }; }
  });
  pi.on('session_start', async () => {
    await call(config, '_hello', {});
  });
}
