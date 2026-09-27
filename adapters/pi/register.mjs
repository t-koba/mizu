/** Version-specific Pi registration, kept separate from transport and policy. */
import { request, typeSchema } from "./bridge-client.mjs";

export function register(pi, Type, config, dependencies = {}) {
  if (config.protocol !== 1) throw new Error("Unsupported Mizu bridge version");
  const call = dependencies.request || request;
  const exit = dependencies.exit || (code => process.exit(code));
  const diagnostic = dependencies.diagnostic || (text => process.stderr.write(text));
  let sequence = 0;
  for (const tool of config.tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: typeSchema(Type, tool.inputSchema),
      executionMode: "sequential",
      async execute(_id, args, signal) {
        const result = await call(config, tool.operation, args, signal);
        return {
          content: [{ type: "text", text: JSON.stringify(result) }],
          details: { operation: tool.operation },
          ...(tool.operation === "finish" ? { terminate: true } : {}),
        };
      },
    });
  }
  pi.on("session_start", async () => {
    pi.setActiveTools(config.tools.map(tool => tool.name));
    await call(config, "_hello", { protocol: 1 });
  });
  pi.on("before_provider_request", async () => {
    try {
      await call(config, "_budget", { sequence: ++sequence });
    } catch {
      diagnostic("Mizu: provider request admission refused\n");
      exit(75); // Do not trust a swallowed hook exception to suppress the request.
    }
  });
  pi.on("cache_warming_decision", () => ({ action: "stop" }));
}
