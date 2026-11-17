import tempfile, unittest
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
