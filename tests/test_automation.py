import tempfile, unittest
from pathlib import Path
from automation import Approvals,Scheduler
class Mock:
 def __init__(self,status='idle'):self.status=status;self.sent=[];self.owned_threads={'t'};self.goal='active';self.fail=False
 def send(self,x):self.sent.append(x)
 def call(self,m,p):
  if m=='thread/read':return {'thread':{'status':{'type':self.status},'updatedAt':1}}
  if m=='thread/goal/get':return {'goal':{'status':self.goal}}
  self.sent.append((m,p))
  if self.fail:raise TimeoutError()
  return {}
class Tests(unittest.TestCase):
 def request(self):return {'id':7,'method':'item/commandExecution/requestApproval','params':{'threadId':'t','command':'echo hello'}}
 def test_approval_expired_idempotent(self):
  a=Approvals();r=Mock();k=a.add(r,self.request(),-1)
  self.assertEqual(a.decide(k,True),'denied');a.decide(k,True);self.assertEqual(len(r.sent),1)
 def test_invalidated_approval(self):
  for mode in ('disconnect','resolve'):
   a=Approvals();r=Mock();k=a.add(r,self.request())
   if mode=='disconnect':a.disconnect(r)
   else:a.resolve(r,7)
   self.assertIn(a.decide(k,True),('disconnected','resolved'));self.assertFalse(r.sent)
 def test_missing_details(self):
  a=Approvals();r=Mock();req=self.request();req['params']={};k=a.add(r,req)
  with self.assertRaises(ValueError):a.decide(k,True)
  self.assertFalse(r.sent)
 def task(self):return {'threadId':'t','enabled':True,'exclusiveOwner':True,'interval':300,'prompt':'continue'}
 def test_scheduler(self):
  s=Scheduler();r=Mock('notLoaded');t=self.task()
  self.assertEqual(s.tick(r,[t],0),['blocked:notLoaded']);r.status='idle'
  self.assertEqual(s.tick(r,[t],301),['sent:t']);self.assertEqual(s.tick(r,[t],602),['blocked:duplicate']);self.assertEqual(len(r.sent),1)
 def test_default_dry_run(self):
  r=Mock();self.assertEqual(Scheduler().tick(r,[{'threadId':'t'}],0),['dry-run:t']);self.assertFalse(r.sent)
 def test_persistent_uncertain_delivery(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'state.db');r=Mock();r.fail=True;s=Scheduler(p)
   self.assertEqual(s.tick(r,[self.task()],0),['backoff:t']);s.db.close();r.fail=False;s=Scheduler(p)
   self.assertEqual(s.tick(r,[self.task()],1000),['blocked:duplicate']);s.db.close();self.assertEqual(len(r.sent),1)
 def test_usage_limited_goal_still_blocks_automatic_continuation(self):
  r=Mock();r.goal='usageLimited';scheduler=Scheduler()
  try:
   self.assertEqual(scheduler.tick(r,[self.task()],0),['blocked:goal'])
   self.assertEqual(scheduler.tick(r,[self.task()],301),['blocked:goal'])
   self.assertFalse(r.sent)
   self.assertEqual(r.goal,'usageLimited')
  finally:scheduler.db.close()
 def test_goal_and_approval_blocks(self):
  r=Mock();r.goal='complete';self.assertEqual(Scheduler().tick(r,[self.task()],0),['blocked:goal'])
  a=Approvals();a.add(r,self.request());r.goal='active';self.assertEqual(Scheduler(approvals=a).tick(r,[self.task()],0),['blocked:approval'])
if __name__=='__main__':unittest.main()
