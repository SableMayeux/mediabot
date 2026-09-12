import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from mediabot.services.local_ai import CONVERSATION_MODELS, LocalAIError, LocalAIService, MODEL, MODEL_DIGEST

URL='http://local-ai-gateway:8080'
ID='12345678-1234-4abc-8def-123456789abc'
MESSAGES=[{'role':'user','content':'An original synthetic question.'}]

def answer(**overrides):
 return {'request_id':ID,'text':'Synthetic answer','model':MODEL,'model_manifest_sha256':MODEL_DIGEST,'retrieval_used':False,'sources':[],'tools_used':[],**overrides}

class Content:
 def __init__(self,raw):self.raw=raw
 async def iter_chunked(self,size):
  for start in range(0,len(self.raw),7):yield self.raw[start:start+7]

class Response:
 def __init__(self,status=200,body=None,error=None,raw=None):
  self.status=status;self.error=error;self.content=Content(raw if raw is not None else json.dumps(body,ensure_ascii=False).encode())
 async def __aenter__(self):
  if self.error:raise self.error
  return self
 async def __aexit__(self,*args):return False

class Session:
 closed=False
 def __init__(self,response):self.response=response;self.calls=[];self.close=AsyncMock()
 def post(self,url,**kwargs):self.calls.append((url,kwargs));return self.response

class SequentialSession(Session):
 def __init__(self,*responses):super().__init__(None);self.responses=iter(responses)
 def post(self,url,**kwargs):self.calls.append((url,kwargs));return next(self.responses)

class LocalAIClientTests(unittest.IsolatedAsyncioTestCase):
 async def test_desktop_requirement_failure_explains_actual_resource_reason_without_replay(self):
  client=self.client({'error':'desktop_unavailable_no_fallback','desktop_reason':'gpu_vram_low'},503)
  with self.assertRaisesRegex(LocalAIError,'GPU memory.*No server request'):
   await client.chat(ID,MESSAGES,profile='conversation',allowed_backends=('desktop',))
  self.assertEqual(len(client.session.calls),1)
  self.assertEqual(client.session.calls[0][1]['json']['allowed_backends'],['desktop'])

 async def test_fallback_reason_only_accepted_for_permitted_auto_server_response(self):
  body=answer(profile='conversation',backend='server',fallback_reason='foreign_gpu_workload')
  client=self.client(body)
  result=await client.chat(ID,MESSAGES,profile='conversation',allowed_backends=('server','desktop'))
  self.assertEqual(result['fallback_reason'],'foreign_gpu_workload')
  for reason in ('private machine details',{},None):
   with self.subTest(reason=reason),self.assertRaisesRegex(LocalAIError,'fallback reason'):
    await self.client({**body,'fallback_reason':reason}).chat(ID,MESSAGES,profile='conversation',allowed_backends=('server','desktop'))
  with self.assertRaisesRegex(LocalAIError,'fallback reason'):
   await self.client(body).chat(ID,MESSAGES,profile='conversation',allowed_backends=('server',))

 def client(self,body=None,status=200,error=None,raw=None):
  result=LocalAIService(base_url=URL);result.session=Session(Response(status,answer() if body is None else body,error,raw));return result
 async def test_complete_chunked_response_and_redirect_boundary(self):
  client=self.client(answer(text='\u00e9'*100))
  result=await client.chat(ID,MESSAGES)
  self.assertEqual(result['text'],'\u00e9'*100)
  self.assertFalse(client.session.calls[0][1]['allow_redirects'])
  self.assertEqual(client.session.calls[0][0],URL+'/v1/chat')
 async def test_only_fixed_private_gateway_urls(self):
  for url in ('https://example.org','http://ollama:11434','http://169.254.169.254','http://local-ai-gateway:8080/secret','http://local-ai-gateway:8080?key=x','http://local-ai-gateway:8080#x','http://user:pass@local-ai-gateway:8080',' http://local-ai-gateway:8080','http://local-ai-gateway:bad','http://local-ai-gateway:8080\n'):
   with self.subTest(url=url),self.assertRaises(LocalAIError):LocalAIService(base_url=url)
  self.assertFalse(LocalAIService(base_url='').enabled)
  self.assertTrue(LocalAIService(base_url='http://127.0.0.1:11888/').enabled)
 async def test_fixed_routes_before_network(self):
  client=self.client()
  with self.assertRaises(LocalAIError):await client.request('/api/pull',{})
  self.assertFalse(client.session.calls)
 async def test_invalid_uuid_never_reaches_network_for_chat_or_cancel(self):
  for value in (None,1,[],{},'bad',ID.upper()):
   client=self.client()
   with self.subTest(value=value):
    with self.assertRaises(LocalAIError):await client.chat(value,MESSAGES)
    with self.assertRaises(LocalAIError):await client.cancel(value)
    self.assertFalse(client.session.calls)
 async def test_invalid_messages_and_options_rejected(self):
  for messages in (None,'text',[],[None],[{'role':[],'content':'x'}],[{'role':'system','content':'x'}],[{'role':'user','content':'x','tools':[]}],[{'role':'user','content':' '}],[{'role':'user','content':'\u00e9'*1501}]):
   client=self.client()
   with self.subTest(messages=messages),self.assertRaises(LocalAIError):await client.chat(ID,messages)
   self.assertFalse(client.session.calls)
 async def test_response_identity_provenance_and_tools_bound(self):
  for change in ({'request_id':str(uuid.uuid4())},{'model':'other'},{'model_manifest_sha256':'changed'},{'retrieval_used':True},{'sources':[{}]},{'tools_used':['tool']},{'text':''}):
   with self.subTest(change=change),self.assertRaises(LocalAIError):await self.client(answer(**change)).chat(ID,MESSAGES)
 async def test_cancellation_tombstone_receipt_and_identity(self):
  body={'request_id':ID,'cancel_requested':True,'active':False}
  self.assertEqual(await self.client(body,202).cancel(ID),body)
  for invalid in ({**body,'request_id':str(uuid.uuid4())},{**body,'cancel_requested':False},{**body,'active':'false'}):
   with self.assertRaises(LocalAIError):await self.client(invalid,202).cancel(ID)
 async def test_no_false_success_for_legacy_cancel_404_or_redirect(self):
  for status in (404,302):
   with self.assertRaises(LocalAIError):await self.client({'error':'missing'},status).cancel(ID)
 async def test_bounded_stream_and_invalid_json(self):
  with self.assertRaisesRegex(LocalAIError,'oversized'):await self.client(raw=b'x'*65537).chat(ID,MESSAGES)
  with self.assertRaises(LocalAIError):await self.client(raw=b'{partial').chat(ID,MESSAGES)
 async def test_specific_errors_and_private_detail_not_echoed(self):
  for status,code,expected in ((429,'busy','another request'),(409,'cancelled','cancelled'),(503,'foreign_gpu_workload','yielding'),(503,'gpu_monitor_stale','safety monitor'),(503,'request_timeout','deadline'),(401,'unauthorized','unavailable')):
   with self.subTest(code=code),self.assertRaisesRegex(LocalAIError,expected) as caught:
    await self.client({'error':code,'detail':'private-secret-value'},status).chat(ID,MESSAGES)
   self.assertNotIn('private-secret-value',str(caught.exception))
 async def test_ambiguous_failure_is_not_retried(self):
  client=self.client(error=asyncio.TimeoutError())
  with self.assertRaises(LocalAIError):await client.chat(ID,MESSAGES)
  self.assertEqual(len(client.session.calls),1)
 async def test_concurrent_start_uses_one_session_and_no_environment_proxy(self):
  with tempfile.TemporaryDirectory() as directory:
   token=Path(directory)/'token';token.write_text('a'*64)
   client=LocalAIService(base_url=URL,token_path=token)
   session=Session(Response(body=answer()))
   with patch('mediabot.services.local_ai.aiohttp.ClientSession',return_value=session) as factory:
    await asyncio.gather(client.start(),client.start())
    factory.assert_called_once();self.assertFalse(factory.call_args.kwargs['trust_env'])
    self.assertEqual(factory.call_args.kwargs['timeout'].total,70)
   await client.close();session.close.assert_awaited_once()

 async def test_conversation_uses_fixed_profile_and_longer_deadline(self):
  client=self.client(answer(profile='conversation'))
  await client.chat(ID,MESSAGES,profile='conversation')
  options=client.session.calls[0][1]
  self.assertEqual(options['json'],{'request_id':ID,'messages':MESSAGES,'profile':'conversation'})
  self.assertEqual(options['timeout'].total,135)
  self.assertFalse(options['allow_redirects'])

 async def test_structured_default_keeps_legacy_wire_contract(self):
  client=self.client()
  await client.chat(ID,MESSAGES)
  options=client.session.calls[0][1]
  self.assertEqual(options['json'],{'request_id':ID,'messages':MESSAGES})
  self.assertNotIn('timeout',options)

 async def test_only_definite_legacy_profile_rejection_retries_once(self):
  client=self.client();client.session=SequentialSession(Response(400,{'error':'invalid_request'}),Response(200,answer()))
  result=await client.chat(ID,MESSAGES,profile='conversation')
  self.assertTrue(result['legacy_profile'])
  first,second=[options for url,options in client.session.calls]
  self.assertEqual(first['json']['profile'],'conversation')
  self.assertEqual(second['json'],{'request_id':ID,'messages':MESSAGES})
  self.assertNotIn('timeout',second)
  client=self.client();client.session=SequentialSession(Response(400,{'error':'invalid_request'}),Response(400,{'error':'invalid_request'}))
  with self.assertRaises(LocalAIError):await client.chat(ID,MESSAGES,profile='conversation')
  self.assertEqual(len(client.session.calls),2)

 async def test_conversation_failures_never_trigger_ambiguous_retry(self):
  for status,code in ((400,'different_error'),(429,'busy'),(503,'request_timeout'),(503,'foreign_gpu_workload'),(401,'unauthorized')):
   client=self.client({'error':code},status)
   with self.subTest(status=status,code=code),self.assertRaises(LocalAIError):
    await client.chat(ID,MESSAGES,profile='conversation')
   self.assertEqual(len(client.session.calls),1)
  client=self.client(error=asyncio.TimeoutError())
  with self.assertRaises(LocalAIError):await client.chat(ID,MESSAGES,profile='conversation')
  self.assertEqual(len(client.session.calls),1)

 async def test_conversation_model_registry_is_pinned_and_structured_stays_llama(self):
  qwen=answer(profile='conversation',model='qwen3.5:4b',model_manifest_sha256=CONVERSATION_MODELS['qwen3.5:4b']['digest'])
  self.assertEqual((await self.client(qwen).chat(ID,MESSAGES,profile='conversation'))['model'],'qwen3.5:4b')
  for body,profile in ((qwen,'structured'),({**qwen,'model_manifest_sha256':'changed'},'conversation'),({**qwen,'model':'unknown'},'conversation'),(answer(profile='structured'),'conversation'),(answer(),'conversation'),(answer(model=[]),'structured')):
   with self.subTest(body=body,profile=profile),self.assertRaises(LocalAIError):
    await self.client(body).chat(ID,MESSAGES,profile=profile)
  for invalid in ('unbounded',None,{},[]):
   client=self.client()
   with self.subTest(invalid=invalid),self.assertRaises(LocalAIError):
    await client.chat(ID,MESSAGES,profile=invalid)
   self.assertFalse(client.session.calls)

if __name__=='__main__':unittest.main()
