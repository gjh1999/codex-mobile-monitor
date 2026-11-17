import copy,tempfile,unittest
from pathlib import Path
from managed_tasks import ManagedTasks
class Mock:
 def __init__(self):self.owned_threads=set();self.calls=[];self.goal=None;self.fail_start=False
 def call(self,m,p):
  self.calls.append((m,p))
  if m=='thread/start':
   if self.fail_start:raise TimeoutError()
   return {'thread':{'id':'created-thread'}}
  if m=='thread/goal/get':return {'goal':self.goal}
  if m=='thread/goal/set':self.goal=p
  return {}
class Tests(unittest.TestCase):
 def job(self):return {'name':'example','cwd':'.','objective':'An explicit configured goal','prompt':'continue that goal','enabled':True,'exclusiveOwner':True}
 def test_create_restore_without_second_creation(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'tasks.db');r=Mock();m=ManagedTasks(p,d);tasks,status=m.sync(r,[self.job()])
   self.assertEqual(status,['managed:example']);self.assertIn('created-thread',r.owned_threads);self.assertEqual(tasks[0]['threadId'],'created-thread')
   m.db.close();m=ManagedTasks(p,d);r2=Mock();r2.goal=r.goal;m.sync(r2,[self.job()]);self.assertNotIn('thread/start',[x[0] for x in r2.calls]);self.assertIn('thread/resume',[x[0] for x in r2.calls]);self.assertNotIn('thread/goal/set',[x[0] for x in r2.calls]);m.db.close()
 def test_uncertain_creation_not_retried(self):
  with tempfile.TemporaryDirectory() as d:
   m=ManagedTasks(str(Path(d)/'tasks.db'),d);r=Mock();r.fail_start=True;m.sync(r,[self.job()]);r.fail_start=False
   self.assertEqual(m.sync(r,[self.job()])[1],['blocked:uncertain_creation:example']);self.assertEqual([x[0] for x in r.calls].count('thread/start'),1);m.db.close()
 def test_disabled_and_outside_workspace(self):
  with tempfile.TemporaryDirectory() as d:
   m=ManagedTasks(str(Path(d)/'tasks.db'),d);r=Mock();j=self.job();j['enabled']=False;self.assertFalse(m.sync(r,[j])[0]);j=self.job();j['cwd']='..';self.assertFalse(m.sync(r,[j])[0]);self.assertFalse(r.calls);m.db.close()
 def test_changed_objective_requires_new_registration(self):
  with tempfile.TemporaryDirectory() as d:
   m=ManagedTasks(str(Path(d)/'tasks.db'),d);r=Mock();m.sync(r,[self.job()]);j=self.job();j['objective']='different'
   self.assertEqual(m.sync(r,[j])[1],['blocked:config_changed:example']);m.db.close()
if __name__=='__main__':unittest.main()
