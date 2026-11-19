import sqlite3, tempfile, unittest
from pathlib import Path
from daemon_support import ScopedPolicy,WindowsNotifications
from reply_log import ReplyLog
from automation import Scheduler
from tests.test_automation import Mock

class Tests(unittest.TestCase):
 def test_policy(self):
  root=Path.cwd();rpc=Mock();rule={'threadId':'t','cwd':str(root),'command':'python --version'}
  policy=ScopedPolicy(root,[rule]);req={'method':'item/commandExecution/requestApproval','params':dict(rule)}
  self.assertTrue(policy.allows(rpc,req))
  for extra in ({'networkApprovalContext': {}}, {'networkApprovalContext': {'host': 'example.org'}},
                {'kind': 'humanCommand'}, {'kind': 'writeStdin'}, {'kind': 'unknown'}):
   self.assertFalse(policy.allows(rpc, {**req, 'params': {**rule, **extra}}))
  self.assertTrue(policy.allows(rpc, {**req, 'params': {**rule, 'kind': 'command', 'networkApprovalContext': None}}))
  for command in ('python --version; curl evil','powershell -Command x','python --version | cmd.exe'):
   req['params']['command']=command;self.assertFalse(policy.allows(rpc,req))
  req['params']=dict(rule);rpc.owned_threads=set();self.assertFalse(policy.allows(rpc,req))
 def test_journal_and_notification_restart(self):
  with tempfile.TemporaryDirectory() as d:
   log=ReplyLog(Path(d)/'r.db');event={'method':'item/completed','params':{'threadId':'t','turnId':'u','item':{'id':'i','type':'agentMessage','text':'full reply'}}}
   self.assertFalse(log.record(event,set()));self.assertTrue(log.record(event,{'t'}));self.assertFalse(log.record(event,{'t'}));log.db.close()
   calls=[];path=Path(d)/'n.db';n=WindowsNotifications(path,lambda *a:calls.append(a) or 'api_called')
   self.assertEqual(n.notify('i','reply','record path'),'api_called');n.db.close()
   n=WindowsNotifications(path,lambda *a:calls.append(a));self.assertEqual(n.notify('i','reply','record path'),'duplicate');self.assertEqual(len(calls),1);n.db.close()
 def test_hour_and_goal_states(self):
  for goal in ('paused','complete','usageLimited'):
   rpc=Mock();rpc.goal=goal;s=Scheduler();self.assertEqual(s.tick(rpc,[{'threadId':'t','enabled':True,'exclusiveOwner':True}],0),['blocked:goal']);s.db.close()
  rpc=Mock('active');s=Scheduler();t={'threadId':'t','enabled':True,'exclusiveOwner':True}
  self.assertEqual(s.tick(rpc,[t],0),['blocked:active']);self.assertEqual(s.tick(rpc,[t],3599),[]);self.assertEqual(s.tick(rpc,[t],3600),['blocked:active']);s.db.close()

 def test_reply_batch_migrates_old_rows_and_replays_interrupted_batch(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'journal.db'
   db=sqlite3.connect(path)
   db.execute('CREATE TABLE replies(thread TEXT, turn TEXT, item TEXT, kind TEXT, text TEXT, received REAL, PRIMARY KEY(thread,turn,item))')
   db.executemany('INSERT INTO replies VALUES(?,?,?,?,?,?)',
                  [('t','u',str(i),'agentMessage',str(i),i) for i in range(5)]+[('t','u','user','userMessage','hi',0)])
   db.commit();db.close()
   sent=[]
   native=WindowsNotifications(Path(folder)/'notifications.db',lambda title,body:sent.append(body) or 'sent')
   native.notify('reply:t:u:0','old','0')
   log=ReplyLog(path)
   try:
    # Failure after durable native delivery, before acknowledging the journal batch.
    class Interrupted:
     def notify(self,*args):
      native.notify(*args)
      raise RuntimeError('interrupted')
    with self.assertRaises(RuntimeError):log.notify_replies(Interrupted(),'title',limit=2)
    log.notify_replies(native,'title',limit=2)
    self.assertEqual(sent,['0','1'])
    log.db.close();log=ReplyLog(path)
    log.record({'method':'item/completed','params':{'threadId':'t','turnId':'live','item':{
        'id':'new','type':'agentMessage','text':'new'}}},{'t'})
    log.notify_replies(native,'title',limit=1)
    self.assertEqual(sent[-1],'new')
    log.notify_replies(native,'title',limit=2)
    log.notify_replies(native,'title',limit=2)
    self.assertIsNone(log.notify_replies(native,'title'))
    self.assertEqual(sent,['0','1','new','2','3','4'])
    self.assertEqual(log.db.execute('SELECT count(*) FROM replies').fetchone(),(7,))
   finally:
    log.db.close();native.db.close()

 def test_owned_turn_events_grant_read_access_only_until_completion(self):
  log=ReplyLog(':memory:');self.addCleanup(log.db.close)
  for start in ('turn/started','item/started'):
   turn=start+'-id'
   params={'threadId':'t','turnId':turn,'turn':{'id':turn}}
   log.record({'method':start,'params':params},{'t'})
   item={'id':'result','type':'agentMessage','text':'final'}
   self.assertTrue(log.record({'method':'item/completed','params':{**params,'item':item}},set()))
   log.record({'method':'turn/completed','params':params},set())
   self.assertFalse(log.record({'method':'item/completed','params':{**params,'item':{**item,'id':'extra'}}},set()))
