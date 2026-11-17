import tempfile,unittest
from pathlib import Path
from reply_log import ReplyLog
from conversation import read_conversation


class Tests(unittest.TestCase):
 def test_messages_are_readable_and_scoped_and_errors_redacted(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'replies.db';log=ReplyLog(path)
   for tid,kind,content in [('t','userMessage',{'content':[{'type':'text','text':'你好'}]}),('t','agentMessage',{'text':'收到，你好！'}),('other','agentMessage',{'text':'其他任务的消息'})]:
    log.record({'method':'item/completed','params':{'threadId':tid,'turnId':'turn','item':{'id':kind, 'type':kind,**content}}},{'t','other'})
   log.record({'method':'error','params':{'threadId':'t','error':{'message':'bad key sk-test_secret123','code':'auth'}}},{'t'})
   log.db.close();result=read_conversation(path,'t')
   self.assertEqual([m['text'] for m in result['messages']],['你好','收到，你好！'])
   self.assertNotIn('其他任务',str(result));self.assertNotIn('sk-test_secret123',str(result))
   self.assertIn('[redacted]',str(result));self.assertEqual(result['events'][0]['method'],'error')
