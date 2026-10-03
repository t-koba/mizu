/** Actual installed Pi SDK + vendor faux provider. No HTTP or paid inference. */
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ModelRuntime, createAgentSession, DefaultResourceLoader, SettingsManager, SessionManager }
  from '../adapters/pi/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { fauxProvider, fauxAssistantMessage, fauxToolCall }
  from '../adapters/pi/node_modules/@earendil-works/pi-ai/dist/index.js';
import { meterRuntime } from '../adapters/pi/model-runtime.mjs';

const directory=await mkdtemp(join(tmpdir(),'mizu-sdk-contract-'));
try {
  const runtime=await ModelRuntime.create({authPath:join(directory,'auth.json'),modelsPath:null,
    modelsStorePath:join(directory,'models.json'),refreshOnCreate:false,allowModelNetwork:false});
  const faux=fauxProvider({provider:'test-only',models:[{id:'model',contextWindow:32768,maxTokens:1024}]});
  runtime.registerNativeProvider(faux.provider);
  const calls=[];
  const meter=meterRuntime(runtime,{},async (_cfg,operation,args)=>{calls.push([operation,args]); return {};});
  faux.setResponses([fauxAssistantMessage('complete')]);
  const result=await runtime.completeSimple(faux.getModel(),{messages:[{role:'user',content:'test',timestamp:Date.now()}]});
  assert.equal(result.stopReason,'stop');
  assert.equal(calls.filter(([op])=>op==='_budget').length,1);
  assert.equal(calls.filter(([op])=>op==='_model_usage').length,1);
  const classifier={...faux.getModel(),id:'classifier',type:'classifier',api:'test-classifier'};
  const image={...faux.getModel(),id:'image',type:'image',api:'test-image',output:['image']};
  const usage={input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
  runtime.registerNativeProvider({...faux.provider,getAllModels:()=>[...faux.models,classifier,image],
    classify:async model=>({provider:model.provider,model:model.id,usage,result:{label:'test'},stopReason:'stop'}),
    generateImages:async model=>({provider:model.provider,model:model.id,usage,images:[],stopReason:'stop'})});
  await Promise.all([runtime.classify(classifier,{}),runtime.generateImages(image,{})]);
  await runtime.setRuntimeApiKey('test-only','synthetic-test-only');
  let routes=0;
  runtime.registerVirtualModel({provider:'test-only',id:'virtual',name:'Virtual',route:async()=>{
    routes++; await runtime.classify(classifier,{});
    return {model:faux.getModel(),thinkingLevel:'off'};
  }});
  faux.setResponses([fauxAssistantMessage('virtual response')]);
  const before=calls.filter(([op])=>op==='_budget').length;
  const virtualResult=await runtime.completeSimple(runtime.getModel('test-only','virtual'),{messages:[]});
  assert.equal(virtualResult.stopReason,'stop');
  assert.equal(routes,1);
  assert.equal(calls.filter(([op])=>op==='_budget').length-before,2);
  const denied=fauxProvider({provider:'denied-test',models:[{id:'denied',contextWindow:4096,maxTokens:32}]});
  const blocked=await ModelRuntime.create({authPath:join(directory,'blocked-auth.json'),modelsPath:null,refreshOnCreate:false});
  blocked.registerNativeProvider(denied.provider);
  meterRuntime(blocked,{},async()=>{throw Error('admission denied');});
  await assert.rejects(blocked.completeSimple(denied.getModel(),{messages:[]}),/admission denied/);
  assert.equal(denied.state.callCount,0);
  const settingsManager=SettingsManager.inMemory({retry:{enabled:false},compaction:{enabled:false}});
  const resourceLoader=new DefaultResourceLoader({cwd:directory,agentDir:directory,settingsManager,
    noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,systemPrompt:'Test-only policy'});
  await resourceLoader.reload();
  let seal=false;
  const {session}=await createAgentSession({cwd:directory,agentDir:directory,modelRuntime:runtime,model:faux.getModel(),
    settingsManager,sessionManager:SessionManager.inMemory(directory),resourceLoader,noTools:'all',tools:['finish'],
    customTools:[{name:'finish',label:'finish',description:'Seal',parameters:{type:'object',properties:{}},exposure:'model-only',
      outputSchema:{type:'object',properties:{sealed:{type:'boolean'}}},
      execute:async()=>{seal=true;return {content:[{type:'text',text:'sealed'}],structuredContent:{sealed:true},details:{},terminate:true};}}]});
  let settled=false;
  session.subscribe(event=>{if(event.type==='agent_settled')settled=true;});
  faux.setResponses([fauxAssistantMessage([fauxToolCall('finish',{})],{stopReason:'toolUse'})]);
  await session.prompt('Run the test finish tool');
  await meter.flush();
  assert.ok(seal && settled);
  await session.dispose();
  console.log(JSON.stringify({status:'pass',kind:'installed-pi-sdk-faux-provider',seal:true,agent_settled:true,auxiliary_and_virtual_metering:true,admission_refusal_before_dispatch:true,paid_inference:'not_run',http:'not_run',oci:'not_run'}));
} finally { await rm(directory,{recursive:true,force:true}); }
