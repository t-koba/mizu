/** Current Pi tool registration. Authority is independent of exposure. */
import { request, typeSchema } from './bridge-client.mjs';

function validatedAuthoritySections(value, required) {
  if (value === undefined && !required) return undefined;
  if (!Array.isArray(value) || value.length < 2 || value.length > 17) {
    throw new Error('Invalid Mizu authority sections');
  }
  const names = new Set();
  let policyCount = 0;
  let charterCount = 0;
  for (let index = 0; index < value.length; index++) {
    const pair = value[index];
    if (!Array.isArray(pair) || pair.length !== 2 ||
        typeof pair[0] !== 'string' || typeof pair[1] !== 'string' || names.has(pair[0])) {
      throw new Error('Invalid Mizu authority sections');
    }
    const [name] = pair;
    if (name === 'mizu_authority_charter') {
      charterCount++;
      if (index !== value.length - 1) throw new Error('Invalid Mizu authority section order');
    } else {
      const match = /^mizu_authority_policy_(0|[1-9][0-9]*)$/.exec(name);
      if (!match || Number(match[1]) !== policyCount) {
        throw new Error('Invalid Mizu authority section order');
      }
      policyCount++;
    }
    names.add(name);
  }
  if (policyCount < 1 || charterCount !== 1 || value.at(-1)[0] !== 'mizu_authority_charter') {
    throw new Error('Invalid Mizu authority sections');
  }
  return value;
}

export function register(pi, Type, config, dependencies = {}) {
  const call = dependencies.request || request;
  const authoritySections = validatedAuthoritySections(dependencies.authoritySections, dependencies.requireAuthority === true);
  if (authoritySections) pi.on('before_agent_start', event => {
    // Use the supported mutable options path, not a forced prompt or a manual
    // SessionManager append (createAgentSession replaces initial system state).
    // Pi diffs these desired sections against replay, persisting positional
    // replacements/removals. Leave every other extension's current sections alone.
    const sections = event.systemPromptOptions.sections;
    for (const name of Object.keys(sections)) {
      if (name.startsWith('mizu_authority_')) delete sections[name];
    }
    // Ordered pairs survive the host's canonical JSON object-key sorting.
    Object.assign(sections, Object.fromEntries(authoritySections));
  });
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
