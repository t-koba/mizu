"""Explicit installed-engine + local mock-provider integration, never paid inference."""
import sys,json,threading,time,dataclasses,os,argparse,subprocess

from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
sys.path.insert(0,str(ROOT/'src'))
from support import Fixture
from mizu.codex import CodexDriver
from mizu.claude import ClaudeDriver
from mizu.pi import PiDriver
from mizu.fs import digest
calls=[]
class Provider(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  calls.append({'path':self.path,'body':body})

  tools=body.get('tools',[])
  name=next((tool.get('name') or tool.get('function',{}).get('name') for tool in tools if 'mizu_finish' in str(tool)),None)
  if not name:
   self.send_response(400);self.end_headers();self.wfile.write(b'{"error":{"type":"invalid_request_error","message":"No finish tool"}}');return
  namespace=None
  for tool in tools:
   if tool.get('type')=='namespace' and 'mizu' in tool.get('name',''):
    namespace=tool['name'];name=next(t['name'] for t in tool['tools'] if 'finish' in t['name'])
  arguments={'outcome':'wait','summary':'Actual SDK with mock provider','state':'No paid inference'}
  if '/messages' in self.path:
   events=[('message_start',{'type':'message_start','message':{'id':'msg_'+str(len(calls)),'type':'message','role':'assistant','content':[],'model':body['model'],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':12,'output_tokens':0}}}),
     ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'tool_'+str(len(calls)),'name':name,'input':{}}}),
     ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(arguments)}}),
     ('content_block_stop',{'type':'content_block_stop','index':0}),
     ('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},'usage':{'output_tokens':5}}),
     ('message_stop',{'type':'message_stop'})]
   # After the sealed tool has returned, provide a final assistant response.
   if any(block.get('type')=='tool_result' for msg in body.get('messages',[])[-1:] for block in (msg.get('content') if isinstance(msg.get('content'),list) else [])):
    events=[('message_start',{'type':'message_start','message':{'id':'msg_'+str(len(calls)),'type':'message','role':'assistant','content':[],'model':body['model'],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':15,'output_tokens':0}}}),
      ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
      ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Finished'}}),
      ('content_block_stop',{'type':'content_block_stop','index':0}),
      ('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':2}}),('message_stop',{'type':'message_stop'})]
  else:
   item={'id':'fc_1','type':'function_call','call_id':'call_1','name':name,'arguments':json.dumps(arguments),'status':'completed'}
   if namespace:item['namespace']=namespace
   if any(item.get('type')=='function_call_output' for item in body.get('input',[])[-1:] if isinstance(item,dict)):
    item={'id':'msg_'+str(len(calls)),'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'Finished','annotations':[]}]}
   response={'id':'resp_'+str(len(calls)),'object':'response','created_at':int(time.time()),'model':body['model'],'status':'completed','output':[item],
     'usage':{'input_tokens':12,'output_tokens':5,'total_tokens':17,'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
   events=[('response.created',{'type':'response.created','response':{**response,'output':[],'status':'in_progress'}}),
    ('response.output_item.added',{'type':'response.output_item.added','output_index':0,'item':item}),
    ('response.output_item.done',{'type':'response.output_item.done','output_index':0,'item':item}),
    ('response.completed',{'type':'response.completed','response':response})]
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  try:
   for event,value in events:self.wfile.write(('event: '+event+'\ndata: '+json.dumps(value)+'\n\n').encode());self.wfile.flush()
  except (BrokenPipeError,ConnectionResetError):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
threading.Thread(target=server.serve_forever,daemon=True).start()
url='http://127.0.0.1:'+str(server.server_port)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--codex',required=True)
parser.add_argument('--claude-python',required=True)
parser.add_argument('--claude-cli',required=True)
parser.add_argument('--pi-node',default='node')
parser.add_argument('--report',type=Path)
args=parser.parse_args()
checks=[]
sessions={}
fixture=Fixture();fixture.setUp()
os.environ['CODEX_AUTH_FILE']=str(fixture.root/'absent-auth.json')
try:
 ctx=fixture.context('consult');profile=ctx.role.profile
 extension=fixture.root/'faux.mjs'
 extension.write_text('import {fauxProvider,fauxAssistantMessage,fauxToolCall} from '+json.dumps(str(ROOT/'adapters/pi/node_modules/@earendil-works/pi-ai/dist/index.js'))+';\nexport default pi=>{const faux=fauxProvider({provider:"test-only",models:[{id:"model",contextWindow:32768,maxTokens:1024}]});pi.registerProvider(faux.provider);faux.setResponses([fauxAssistantMessage([fauxToolCall("mizu_finish",{outcome:"wait",summary:"Actual managed SDK",state:"mock only"})],{stopReason:"toolUse"})]);};')
 raw={**fixture.config.profiles[profile],'provider':'test-only','model':'model','resources':[{'kind':'extension','path':str(extension),'sha256':digest(extension.read_bytes())}],'options':{'thinkingLevel':'off','settings':{'retry':{'enabled':False},'compaction':{'enabled':False}}}}
 cfg=dataclasses.replace(fixture.config,profiles={**fixture.config.profiles,profile:raw},engines={**fixture.config.engines,'pi':{**fixture.config.engines['pi'],'command':(args.pi_node,)}})
 ctx.config=cfg
 try:
  result=PiDriver(cfg).execute(ctx,'Call mizu_finish.')
  checks.append({'engine':'pi','status':'pass','request_unit':result['request_unit'],'admissions':result['requests'],'usage_known':result['usage_known'],'seal':ctx.finished is not None,'provider':'vendor-faux','mock_http_requests':0})
 except Exception as exc:
  checks.append({'engine':'pi','status':'fail','error_type':type(exc).__name__})
 for engine in ('codex','claude','codex','claude'):
  ctx=fixture.context('consult');profile=ctx.role.profile
  options={'model_providers':{'mock':{'name':'mock','base_url':url,'wire_api':'responses','experimental_bearer_token':'synthetic','request_max_retries':0,'stream_max_retries':0}},'features':{'responses_websockets':False}} if engine=='codex' else {'cli_path':args.claude_cli,'env':{'ANTHROPIC_API_KEY':'synthetic','ANTHROPIC_BASE_URL':url,'HOME':str(fixture.root),'XDG_CONFIG_HOME':str(fixture.root),'XDG_DATA_HOME':str(fixture.root)}}
  raw={**fixture.config.profiles[profile],'engine':engine,'provider':'mock' if engine=='codex' else 'anthropic','model':'claude-sonnet-4-5-20250929','session':'persistent','options':options}
  command=(args.codex,) if engine=='codex' else (args.claude_python,)
  cfg=dataclasses.replace(fixture.config,profiles={**fixture.config.profiles,profile:raw},engines={**fixture.config.engines,engine:{**fixture.config.engines[engine],'command':command}})
  ctx.ephemeral=False;ctx.config=cfg;ctx.deadline=time.monotonic()+30
  before=len(calls)
  try:
   result=(CodexDriver(cfg) if engine=='codex' else ClaudeDriver(cfg)).execute(ctx,'Call mizu_finish now. This is a test.')
   identifier=result.get('session_id',result.get('conversation_id'))
   resumed=engine in sessions
   if resumed and sessions[engine]!=identifier:raise AssertionError('Resume changed session identifier')
   sessions[engine]=identifier
   checks.append({'engine':engine,'resumed':resumed,'status':'pass','request_unit':result['request_unit'],'admissions':result['requests'],'usage_known':result['usage_known'],'usage':result['usage'],'seal':ctx.finished is not None,'mock_http_requests':len(calls)-before})
  except Exception as exc:
   checks.append({'engine':engine,'status':'fail','error_type':type(exc).__name__})
finally:
 server.shutdown();fixture.doCleanups()

receipt={'kind':'installed-engines-local-mock-provider','checks':checks,'paid_inference':'not_run','oci':'not_run','provider_credentials':'synthetic','versions':{engine:subprocess.check_output([command,'--version'],text=True).strip() for engine,command in [('codex',args.codex),('claude',args.claude_cli)]}}
if args.report:
 args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
raise SystemExit(int(any(c['status']!='pass' for c in checks)))
