/** Offline schema conversion only. The caller supplies the Python-owned catalog. */
import { typeSchema as pi } from '../adapters/pi/bridge-client.mjs';
import { typeSchema as durable } from '../adapters/pi-durable/bridge-client.mjs';

const MAX_BYTES = 2 * 1024 * 1024;
const pointer = name => String(name).replaceAll('~', '~0').replaceAll('/', '~1');

function children(schema, path) {
  if (!schema || typeof schema !== 'object') return [];
  const entries = Object.entries(schema.properties ?? {}).map(([name, child]) =>
    [child, `${path}/properties/${pointer(name)}`]);
  if (schema.items !== undefined) entries.push([schema.items, `${path}/items`]);
  if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
    entries.push([schema.additionalProperties, `${path}/additionalProperties`]);
  }
  return entries;
}

function failure(convert, schema, path = '#') {
  try {
    convert(schema);
    return null;
  } catch (error) {
    // The bridges currently throw without paths. Find the first failing child
    // using that same converter, not an independent schema validator.
    if (typeof error.path === 'string') return { path: error.path, message: error.message };
    for (const [child, childPath] of children(schema, path)) {
      const nested = failure(convert, child, childPath);
      if (nested) return nested;
    }
    return { path, message: error.message };
  }
}

try {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > MAX_BYTES) throw new Error('schema manifest exceeds byte bound');
    chunks.push(chunk);
  }
  const manifest = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const results = [];
  for (const [adapter, convert] of [['pi', pi], ['pi-durable', durable]]) {
    for (const [tool, schema] of Object.entries(manifest)) {
      try {
        results.push({ adapter, tool, schema: convert(schema) });
      } catch {
        results.push({ adapter, tool, error: failure(convert, schema) });
      }
    }
  }
  const output = JSON.stringify(results);
  if (Buffer.byteLength(output) > MAX_BYTES) throw new Error('schema results exceed byte bound');
  process.stdout.write(output);
} catch (error) {
  process.stderr.write(`${String(error.message).slice(0, 1024)}\n`);
  process.exitCode = 1;
}
