/** A deliberately small, bounded, LF-framed bridge client.
 * Connects over a Unix socket path or a loopback TCP host/port, as recorded
 * in bridge.json. Anything else is refused without connecting. */
import net from "node:net";

export function request(config, operation, args, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new Error("Cancelled"));
    let endpoint;
    if (config.socket) endpoint = { path: config.socket };
    else if (config.host && config.port) endpoint = { host: config.host, port: config.port };
    else return reject(new Error("Bridge endpoint is not configured"));
    let bytes = 0;
    let chunks = [];
    let settled = false;
    const socket = net.createConnection(endpoint);
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abort);
      socket.destroy();
      error ? reject(error) : resolve(value);
    };
    const abort = () => finish(new Error("Cancelled"));
    signal?.addEventListener("abort", abort, { once: true });
    socket.setTimeout(config.timeout_ms, () => finish(new Error("Bridge deadline exceeded")));
    socket.on("error", error => finish(error));
    socket.on("end", () => finish(new Error("Bridge closed before responding")));
    socket.on("connect", () => {
      const wire = JSON.stringify({ token: config.token, operation, arguments: args }) + "\n";
      if (Buffer.byteLength(wire) > 1048576) return finish(new Error("Request exceeds byte limit"));
      socket.write(wire);
    });
    socket.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > 2097152) return finish(new Error("Response exceeds byte limit"));
      chunks.push(chunk);
      if (!chunk.includes(10)) return;
      const buffer = Buffer.concat(chunks);
      const end = buffer.indexOf(10);
      try {
        const response = JSON.parse(buffer.subarray(0, end).toString("utf8"));
        if (!response.ok) return finish(new Error(response.error || "Tool refused"));
        finish(null, response.result);
      } catch (error) {
        finish(error);
      }
    });
  });
}

/** Rebuild the tiny schema subset with Pi's version-matched TypeBox instance. */
export function typeSchema(Type, schema) {
  if (schema.enum) return Type.Union(schema.enum.map(value => Type.Literal(value)));
  if (schema.type === "string") return Type.String({ maxLength: schema.maxLength });
  if (schema.type === "integer") return Type.Integer({ minimum: schema.minimum, maximum: schema.maximum });
  if (schema.type === "boolean") return Type.Boolean();
  if (schema.type === "array") return Type.Array(typeSchema(Type, schema.items), { maxItems: schema.maxItems });
  if (schema.type === "object") {
    const properties = Object.fromEntries(Object.entries(schema.properties).map(([name, child]) => [
      name, (schema.required || []).includes(name)
        ? typeSchema(Type, child) : Type.Optional(typeSchema(Type, child)),
    ]));
    return Type.Object(properties, { additionalProperties: false });
  }
  throw new Error(`Unsupported tool schema: ${schema.type}`);
}
