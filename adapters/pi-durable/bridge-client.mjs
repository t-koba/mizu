/** Vendored bounded bridge client (provenance: adapters/pi/bridge-client.mjs).
 *
 * Kept as a local copy so pi-durable never imports across adapters; the two
 * copies implement the same small LF-framed Unix/loopback protocol. Connects
 * over a Unix socket path or a loopback TCP host/port, as recorded in
 * bridge.json. Anything else is refused without connecting.
 */
import net from "node:net";

export function request(config, operation, args, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new Error("Cancelled"));
    let endpoint;
    if (config.transport === "unix" && typeof config.socket === "string" && config.socket) endpoint = { path: config.socket };
    else if (config.transport === "tcp" && ["127.0.0.1", "::1"].includes(config.host) && Number.isInteger(config.port) && config.port > 0 && config.port < 65536) endpoint = { host: config.host, port: config.port };
    else return reject(new Error("Bridge endpoint is not configured"));
    let bytes = 0;
    let chunks = [];
    let settled = false;
    let deadline;
    const joining = config.joined_operations?.includes(operation) === true;
    const socket = net.createConnection(endpoint);
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      signal?.removeEventListener("abort", abort);
      socket.destroy();
      error ? reject(error) : resolve(value);
    };
    const abort = () => finish(new Error("Cancelled"));
    signal?.addEventListener("abort", abort, { once: true });
    // Absolute deadline: socket.setTimeout is idle-only and a trickling peer
    // would defer it indefinitely, so arm a wall-clock timer instead.
    const expires = performance.now() + config.timeout_ms;
    const expire = () => {
      const remaining = expires - performance.now();
      if (remaining <= 0) return finish(new Error("Bridge deadline exceeded"));
      // Node clamps oversized timer delays to 1 ms. Chunk the wait, never
      // renew the absolute deadline (including for trickling peers).
      deadline = setTimeout(expire, Math.min(remaining, 2**31 - 1));
    };
    expire();
    socket.on("error", error => finish(error));
    socket.on("end", () => finish(new Error("Bridge closed before responding")));
    socket.on("connect", () => {
      const wire = JSON.stringify({ token: config.token, operation, arguments: args }) + "\n";
      if (Buffer.byteLength(wire) > 1048576) return finish(new Error("Request exceeds byte limit"));
      socket.write(wire);
      // An admitted synchronous join is bounded by the host's child-local
      // deadlines/cancellation. Before admission its host transport timer
      // still applies. A caller-wide RPC timer must not shorten the child.
      if (joining) clearTimeout(deadline);
    });
    socket.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > 1048576) return finish(new Error("Response exceeds byte limit"));
      chunks.push(chunk);
      if (!chunk.includes(10)) return;
      const buffer = Buffer.concat(chunks);
      const end = buffer.indexOf(10);
      try {
        const response = JSON.parse(buffer.subarray(0, end).toString("utf8"));
        if (!response || typeof response !== "object" || typeof response.ok !== "boolean") return finish(new Error("Invalid bridge envelope"));
        if (!response.ok) return finish(new Error(response.error || "Tool refused"));
        if (!response.result || typeof response.result !== "object" || Array.isArray(response.result)) return finish(new Error("Invalid bridge result"));
        finish(null, response.result);
      } catch (error) {
        finish(error);
      }
    });
  });
}

/** Validate the tiny schema subset the bridge emits as plain JSON Schema.
 * Pi compiles it with coercion; anything outside this subset still fails
 * closed instead of reaching the model as an unchecked schema. */
const SCHEMA_TYPES = new Set(["string", "integer", "boolean", "array", "object"]);

function unsupportedSchema(schema) {
  return new Error(`Unsupported tool schema: ${schema?.type ?? "missing type"}`);
}

export function typeSchema(schema) {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) throw unsupportedSchema(schema);
  if (schema.type !== undefined && !SCHEMA_TYPES.has(schema.type)) throw unsupportedSchema(schema);
  if (schema.enum !== undefined) {
    if (!Array.isArray(schema.enum) || schema.enum.length === 0) throw unsupportedSchema(schema);
    return { ...(schema.type === undefined ? {} : { type: schema.type }), enum: [...schema.enum] };
  }
  if (schema.type === "string") {
    const result = { type: "string" };
    for (const key of ["minLength", "maxLength", "pattern"]) {
      if (schema[key] !== undefined) result[key] = schema[key];
    }
    return result;
  }
  if (schema.type === "integer") {
    const result = { type: "integer" };
    for (const key of ["minimum", "maximum"]) {
      if (schema[key] !== undefined) result[key] = schema[key];
    }
    return result;
  }
  if (schema.type === "boolean") return { type: "boolean" };
  if (schema.type === "array") {
    const result = { type: "array", items: typeSchema(schema.items) };
    for (const key of ["minItems", "maxItems"]) {
      if (schema[key] !== undefined) result[key] = schema[key];
    }
    if (schema.uniqueItems === true) result.uniqueItems = true;
    return result;
  }
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) throw unsupportedSchema(schema);
  const result = { type: "object", properties: Object.fromEntries(Object.entries(properties).map(
    ([name, child]) => [name, typeSchema(child)])) };
  if (schema.required !== undefined) {
    if (!Array.isArray(schema.required)
        || schema.required.some(name => typeof name !== "string" || !Object.hasOwn(properties, name))) {
      throw unsupportedSchema(schema);
    }
    result.required = [...schema.required];
  }
  for (const key of ["minProperties", "maxProperties"]) {
    if (schema[key] !== undefined) result[key] = schema[key];
  }
  result.additionalProperties = false;
  return result;
}
