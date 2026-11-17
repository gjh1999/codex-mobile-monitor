import unittest
from attachments import Attachments
from rpc_client import RPCError
TARGET='01a1cad9-8028-7771-bf25-2115a5c73e1f'


class Mock:
 def __init__(self,fail=False):self.calls=[];self.fail=fail;self.owned_threads=set()
 def call(self,method,params):
  self.calls.append(method)
  if self.fail:raise RPCError(code=-32600,detail='no rollout found for thread id')
  return {'thread':{'id':TARGET,'status':{'type':'idle'}}} if method=='thread/resume' else {'goal':{'status':'usageLimited'}}


class Tests(unittest.TestCase):
 def test_loads_automatically_without_starting_or_claiming_ownership(self):
  clock=[0];tracker=Attachments(lambda:clock[0]);rpc=Mock()
  result=tracker.sync(rpc,[TARGET])[0]
  self.assertEqual(result['status'],'attached');self.assertEqual(result['goalStatus'],'usageLimited')
  self.assertFalse(result['automaticContinue']);self.assertFalse(rpc.owned_threads)
  self.assertEqual(rpc.calls,['thread/resume','thread/goal/get'])
  tracker.sync(rpc,[TARGET]);self.assertEqual(len(rpc.calls),2)
 def test_failed_load_retries_with_backoff_and_on_reconnect(self):
  clock=[0];tracker=Attachments(lambda:clock[0]);rpc=Mock(True)
  self.assertEqual(tracker.sync(rpc,[TARGET])[0]['status'],'failed')
  clock[0]=299;tracker.sync(rpc,[TARGET]);self.assertEqual(len(rpc.calls),1)
  clock[0]=300;tracker.sync(rpc,[TARGET]);self.assertEqual(len(rpc.calls),2)
  replacement=Mock();self.assertEqual(tracker.sync(replacement,[TARGET])[0]['status'],'attached')
 def test_invalid_identifier_is_not_sent(self):
  rpc=Mock();self.assertEqual(Attachments().sync(rpc,['../../bad']),[]);self.assertEqual(rpc.calls,[])
