/** Installed Pi 1.6.0 selects extension/MCP tools by namespace. Faux model, no network. */
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ModelRuntime, createAgentSession, createCodemodeExtension, createMcpExtension,
  createToolSearchExtension, DefaultResourceLoader, SessionManager, SettingsManager }
  from '../adapters/pi/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { fauxProvider, fauxAssistantMessage, fauxToolCall }
  from '../adapters/pi/node_modules/@earendil-works/pi-ai/dist/index.js';
import { createInMemoryTransportPair }
  from '../adapters/pi/node_modules/@earendil-works/pi-mcp/dist/testing/index.js';
import { extensionFactories, sessionToolSelection } from '../adapters/pi/extension-factories.mjs';

const directory = await mkdtemp(join(tmpdir(), 'mizu-pi-namespace-'));
let session;
try {
  const runtime = await ModelRuntime.create({ authPath: join(directory, 'auth.json'), modelsPath: null,
    modelsStorePath: join(directory, 'models.json'), refreshOnCreate: false, allowModelNetwork: false });
  const faux = fauxProvider({ provider: 'test-only', models: [{ id: 'model', contextWindow: 32768, maxTokens: 1024 }] });
  runtime.registerNativeProvider(faux.provider);

  const cfg = {
    options: { codemode: true, toolSearch: true, excludeTools: [], scopedModels: undefined },
    mcp_servers: { guides: { type: 'stdio', command: 'in-memory-fixture', args: [], exposure: 'codemode',
      description: 'Offline guide lookup for the namespace check.' } },
    engine_tools: [], authority_sections: undefined, promptCwd: undefined,
  };
  // noTools:'builtin' replaces the settings defaultTools loadout, so the fixture grant
  // list includes the two SDK selection tools alongside the namespaced fixture tool.
  const bridge = { tools: [{ name: 'find_reference' }, { name: 'codemode' }, { name: 'tool_search' }] };

  // A local MCP server: the transport factory injects its client, so no stdio process or HTTP.
  const mcpCalls = [];
  const createTransport = () => {
    const { client, server } = createInMemoryTransportPair();
    server.onMessage(async message => {
      if (!('method' in message) || !('id' in message)) return; // notifications get no reply
      let result;
      if (message.method === 'initialize') {
        result = { protocolVersion: message.params?.protocolVersion ?? '2025-11-25',
          capabilities: { tools: {} }, serverInfo: { name: 'guides-fixture', version: '1.0.0' },
          instructions: 'Use find_guide to locate an offline guide page.' };
      } else if (message.method === 'tools/list') {
        result = { tools: [{ name: 'find_guide', description: 'Find an offline guide page by topic.',
          inputSchema: { type: 'object', properties: { topic: { type: 'string' } }, required: ['topic'] } }] };
      } else if (message.method === 'tools/call') {
        mcpCalls.push({ name: message.params?.name, arguments: message.params?.arguments });
        result = { content: [{ type: 'text', text: `guide page for ${message.params?.arguments?.topic}` }] };
      } else {
        await server.send({ jsonrpc: '2.0', id: message.id,
          error: { code: -32601, message: `unsupported method ${message.method}` } });
        return;
      }
      await server.send({ jsonrpc: '2.0', id: message.id, result });
    });
    void server.start(); // start the in-memory peer as well as the injected client
    return client;
  };

  const fixtureRegister = pi => {
    pi.registerTool({
      name: 'find_reference', label: 'Find reference',
      description: 'Find a local documentation reference by topic.',
      parameters: { type: 'object', properties: { topic: { type: 'string' } }, required: ['topic'], additionalProperties: false },
      exposure: 'codemode',
      namespace: { name: 'mcp__docs', aliases: ['docs'], description: 'Local documentation fixture.',
        instructions: 'Call find_reference for local documentation topics.' },
      execute: async (_id, params) => ({ content: [{ type: 'text', text: `reference for ${params.topic}` }], details: {} }),
    });
  };
  const sdk = { register: fixtureRegister, createCodemodeExtension, createToolSearchExtension, createMcpExtension };
  const mcpOptions = { createTransport, logPath: join(directory, 'mcp.log') };
  const factories = extensionFactories(cfg, bridge, sdk, mcpOptions);
  const selection = sessionToolSelection(cfg, bridge);
  assert.deepEqual(factories.map(f => f.name), ['mizu', 'codemode', 'tool-search', 'mcp']);
  assert.equal(selection.noTools, 'builtin');
  assert.deepEqual(selection.tools, ['find_reference', 'codemode', 'tool_search']);
  const minimalCfg = { ...cfg, options: { ...cfg.options, codemode: false, toolSearch: false }, mcp_servers: {} };
  assert.deepEqual(extensionFactories(minimalCfg, bridge, sdk).map(f => f.name), ['mizu']);

  const settingsManager = SettingsManager.inMemory({ retry: { enabled: false }, compaction: { enabled: false },
    defaultTools: ['codemode', 'tool_search'] });
  const resourceLoader = new DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager,
    noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: factories, systemPrompt: 'Offline namespace check.' });
  await resourceLoader.reload();
  assert.deepEqual(resourceLoader.getExtensions().errors, []);
  ({ session } = await createAgentSession({ cwd: directory, agentDir: directory, modelRuntime: runtime,
    model: faux.getModel(), settingsManager, sessionManager: SessionManager.inMemory(directory),
    resourceLoader, ...selection }));
  // Keep the namespaced fixture tool deferred so tool_search selects it, as in the
  // original no-allowlist setup; the SDK otherwise activates explicitly named tools.
  session.setActiveToolsByName(['codemode', 'tool_search']);
  await session.bindExtensions({});

  const namespaceOf = name => session.getAllTools().find(tool => tool.name === name);
  const extensionTool = namespaceOf('find_reference');
  assert.ok(extensionTool, 'namespaced extension tool is registered');
  assert.equal(extensionTool.exposure, 'codemode');
  assert.equal(extensionTool.namespace?.name, 'mcp__docs');
  assert.deepEqual(extensionTool.namespace.aliases, ['docs']);
  assert.equal(extensionTool.namespace.instructions, 'Call find_reference for local documentation topics.');
  assert.equal(extensionTool.name, 'find_reference', 'no legacy namespace prefix on the fixture tool');
  let mcpTool;
  for (let attempt = 0; attempt < 100 && !mcpTool; attempt++) {
    mcpTool = namespaceOf('mcp__guides__find_guide');
    if (!mcpTool) await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.ok(mcpTool, 'MCP server tool is registered as mcp__guides__find_guide');
  assert.equal(mcpTool.exposure, 'deferred', 'MCP codemode exposure maps to deferred tool exposure');
  assert.equal(mcpTool.namespace?.name, 'mcp__guides');
  assert.deepEqual(mcpTool.namespace.aliases, ['guides']);
  assert.equal(mcpTool.namespace.instructions, 'Use find_guide to locate an offline guide page.');

  const results = {};
  let settled = false;
  session.subscribe(event => {
    if (event.type === 'tool_execution_end' && !event.parentToolCallId)
      (results[event.toolName] ??= []).push((event.result?.content ?? []).filter(block => block.type === 'text').map(block => block.text).join('\n'));
    if (event.type === 'agent_settled') settled = true;
  });
  const code = `
const found = await searchTools('find guide reference', { limit: 8 });
const scoped = await searchTools('find', { namespace: 'docs', limit: 8 });
const docs = await describeNamespace('docs');
const guides = await describeNamespace('mcp__guides');
const reference = await tools.find_reference({ topic: 'namespaces' });
const guide = await tools.mcp__guides__find_guide({ topic: 'namespaces' });
return JSON.stringify({ found: found.map(t => t.name), scoped: scoped.map(t => t.name), docs, guides,
  reference, guide, all: ALL_TOOLS.map(t => t.name) });
`;
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('codemode', { code })], { stopReason: 'toolUse' }),
    fauxAssistantMessage([fauxToolCall('tool_search', { query: 'find guide reference', limit: 8 })], { stopReason: 'toolUse' }),
    fauxAssistantMessage('namespace check complete'),
  ]);
  await session.prompt('Find and select the namespaced documentation tools.');
  assert.ok(settled, 'agent turn settled');
  const scriptOutput = results.codemode?.at(-1) ?? '';
  assert.ok(!/Script failed|Script error/.test(scriptOutput), `codemode script failed: ${scriptOutput}`);
  const json = scriptOutput.match(/\{[\s\S]*\}/)?.[0];
  assert.ok(json, `codemode output has no JSON: ${scriptOutput}`);
  const selected = JSON.parse(json);
  for (const name of ['find_reference', 'mcp__guides__find_guide']) {
    assert.ok(selected.found.includes(name), `searchTools ranked ${name}: ${scriptOutput}`);
    assert.ok(selected.all.includes(name), `ALL_TOOLS contains ${name}: ${scriptOutput}`);
  }
  assert.deepEqual(selected.scoped, ['find_reference'], 'searchTools namespace filter uses the alias');
  assert.equal(selected.docs.name, 'mcp__docs');
  assert.deepEqual(selected.docs.tools, ['find_reference']);
  assert.equal(selected.guides.name, 'mcp__guides');
  assert.deepEqual(selected.guides.tools, ['mcp__guides__find_guide']);
  assert.equal(selected.guides.instructions, 'Use find_guide to locate an offline guide page.');
  assert.equal(selected.reference, 'reference for namespaces');
  assert.equal(selected.guide.content?.[0]?.text, 'guide page for namespaces');
  assert.deepEqual(mcpCalls, [{ name: 'find_guide', arguments: { topic: 'namespaces' } }]);
  const searchOutput = results.tool_search?.at(-1) ?? '';
  assert.ok(searchOutput.includes('find_reference') && searchOutput.includes('mcp__guides__find_guide'),
    `tool_search did not load both namespaced tools: ${searchOutput}`);
  for (const name of ['find_reference', 'mcp__guides__find_guide'])
    assert.ok(session.getActiveToolNames().includes(name), `tool_search activated ${name}`);
  assert.equal(faux.state.callCount, 3, 'three scripted faux turns, no other inference');
  console.log(JSON.stringify({ status: 'pass', kind: 'pi-namespace-selection', sdk_version: '1.6.0',
    extension_namespace: 'mcp__docs', mcp_namespace: 'mcp__guides', mcp_tool: 'mcp__guides__find_guide',
    codemode_selection: true, tool_search_selection: true, legacy_namespace_options: false,
    paid_inference: 'not_run', http: 'not_run' }));
} finally {
  await session?.dispose();
  await rm(directory, { recursive: true, force: true });
}
