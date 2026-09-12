import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import uuid

BASE=Path(__file__).resolve().parents[1]
def module(name, filename):
 spec=importlib.util.spec_from_file_location(name,BASE/'bin'/filename)
 result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
G=module('gateway','gateway.py');W=module('watch','gpu-watch.py')

class BoundaryTests(unittest.TestCase):
 def payload(self,text='Hello'):
  return {'request_id':str(uuid.uuid4()),'messages':[{'role':'user','content':text}]}
 def test_valid_text(self):
  body=self.payload();self.assertEqual(G.validate(body),(body['request_id'],body['messages']))
 def test_model_tools_options_and_system_rejected(self):
  for key in ('model','options','tools','keep_alive','stream'):
   body=self.payload();body[key]='override'
   with self.assertRaises(ValueError):G.validate(body)
  body=self.payload();body['messages'][0]['role']='system'
  with self.assertRaises(ValueError):G.validate(body)
 def test_multibyte_limit(self):
  with self.assertRaises(ValueError):G.validate(self.payload('\u00e9'*1501))
 def test_transport_timeout_reports_deadline(self):
  connection=MagicMock();connection.request.side_effect=TimeoutError()
  with patch.object(G.http.client,'HTTPConnection',return_value=connection),patch.object(G.time,'monotonic',side_effect=[0,61]):
   with self.assertRaisesRegex(RuntimeError,'request_timeout'):
    G.chat(G.Job(str(uuid.uuid4())),[{'role':'user','content':'test'}])
 def test_cancel_lease_identity_expiry_and_bound(self):
  G.CANCELLED.clear();first=str(uuid.uuid4())
  with patch.object(G.time,'monotonic',return_value=100):
   G.remember_cancel(first)
   self.assertTrue(G.cancelled_before_admission(first))
   self.assertFalse(G.cancelled_before_admission(str(uuid.uuid4())))
   for _ in range(300):G.remember_cancel(str(uuid.uuid4()))
   self.assertEqual(len(G.CANCELLED),256)
  with patch.object(G.time,'monotonic',return_value=161):
   self.assertFalse(G.cancelled_before_admission(first));self.assertEqual(G.CANCELLED,{})
 def test_request_id_must_be_uuid_text(self):
  for value in (None,1,False,{},[], 'invalid'):
   body=self.payload();body['request_id']=value
   with self.assertRaises(ValueError):G.validate(body)
 def test_cancel_wakes_socket_without_cross_thread_response_close(self):
  job=G.Job(str(uuid.uuid4()));job.connection=MagicMock()
  job.cancel()
  self.assertTrue(job.cancelled.is_set())
  job.connection.sock.shutdown.assert_called_once()
  job.connection.close.assert_not_called()
 def test_owned_stop_survives_reload_but_manual_or_replaced_runtime_does_not(self):
  with tempfile.TemporaryDirectory() as temporary:
   directory=Path(temporary);identity='1'*64
   self.assertFalse(W.owns_stop(directory,identity))
   if not hasattr(W.os,'O_DIRECTORY'):
    self.skipTest('Directory durability is verified on Linux.')
   W.mark_owned_stop(directory,identity)
   reloaded=module('watch_reload','gpu-watch.py')
   self.assertTrue(reloaded.owns_stop(directory,identity))
   self.assertFalse(reloaded.owns_stop(directory,'2'*64))
   reloaded.clear_owned_stop(directory,identity)
   self.assertFalse(reloaded.owns_stop(directory,identity))
 def test_aborted_transport_reports_resource_interlock_reason(self):
  connection=MagicMock();connection.request.side_effect=ConnectionResetError()
  with patch.object(G.http.client,'HTTPConnection',return_value=connection),patch.object(G,'guard_state',return_value=(False,'foreign_gpu_workload')):
   with self.assertRaisesRegex(RuntimeError,'foreign_gpu_workload'):
    G.chat(G.Job(str(uuid.uuid4())),[{'role':'user','content':'test'}])
 def test_empty_tail(self):
  with self.assertRaises(ValueError):G.validate(self.payload(' '))
 def test_gpu_idle_admission(self):
  state=W.classify([7932,0,0,0,48],set(),15929)
  self.assertTrue(state['healthy']);self.assertTrue(state['admit'])
 def test_existing_media_gpu_work_rejects(self):
  for gpu,foreign in [([7932,0,0,0,48],{123}),([7932,0,2,0,48],set()),([7932,0,0,2,48],set())]:
   state=W.classify(gpu,foreign,15929)
   self.assertFalse(state['healthy']);self.assertFalse(state['admit'])
 def test_inference_headroom_and_hard_stop_threshold(self):
  self.assertTrue(W.classify([5000,99,0,0,60],set(),10000)['healthy'])
  self.assertFalse(W.classify([5000,99,0,0,60],set(),10000)['admit'])
  self.assertFalse(W.classify([4095,50,0,0,60],set(),10000)['healthy'])
 def test_ram_and_temperature_fail_closed(self):
  self.assertFalse(W.classify([7932,0,0,0,80],set(),15929)['healthy'])
  self.assertFalse(W.classify([7932,0,0,0,48],set(),6143)['healthy'])
 def test_stale_missing_future_or_invalid_monitor_rejected(self):
  old=G.STATE
  with tempfile.TemporaryDirectory() as temporary:
   G.STATE=Path(temporary)/'status.json'
   self.assertFalse(G.guard_state()[0])
   for value in ('no',json.dumps({'observed_at':time.time()-10,'healthy':True,'admit':True}),json.dumps({'observed_at':time.time()+10,'healthy':True,'admit':True})):
    G.STATE.write_text(value);self.assertFalse(G.guard_state()[0])
   G.STATE.write_text(json.dumps({'observed_at':time.time(),'healthy':True,'admit':True}))
   self.assertTrue(G.guard_state(True)[0])
  G.STATE=old

if __name__=='__main__':unittest.main()