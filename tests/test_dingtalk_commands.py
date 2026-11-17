import copy, json, unittest
from automation import Approvals
from dingtalk_commands import CommandRouter
class RPC:
 def __init__(self):self.sent=[]
 def send(self,x):self.sent.append(x)
class Tests(unittest.TestCase):
 def setUp(self):
  self.now=100;self.a=Approvals(clock=lambda:self.now);self.rpc=RPC()
  self.token=self.a.add(self.rpc,{'id':1,'method':'item/commandExecution/requestApproval','params':{'command':'echo reviewed','threadId':'t'}})
  self.router=CommandRouter(self.a,lambda:{'connection':'connected','privateSecret':'never return'},[{'corpId':'corp','staffId':'staff','conversationId':'conversation'}],clock=lambda:self.now)
  self.seq=0
 def message(self,text,**overrides):
  self.seq+=1;d={'senderCorpId':'corp','senderStaffId':'staff','conversationId':'conversation','conversationType':'1','msgId':str(self.seq),'msgtype':'text','text':{'content':text}};d.update(overrides);return d
 def view(self):
  result=self.router.handle(self.message('查看 '+self.token));return result.split('批准 ')[1].split('\n')[0]
 def test_identity_and_private_conversation(self):
  for key in ('senderCorpId','senderStaffId','conversationId','conversationType'):
   self.assertIsNone(self.router.handle(self.message('状态',**{key:'wrong'})))
  self.assertFalse(self.rpc.sent)
 def test_safe_status(self):
  result=self.router.handle(self.message('状态'));self.assertNotIn('privateSecret',result);self.assertIn('connected',result)
 def test_view_before_allow_and_single_use(self):
  self.assertIn('无效',self.router.handle(self.message('批准 '+self.token+' fake')));self.assertFalse(self.rpc.sent)
  command=self.view();self.assertIn('allowed',self.router.handle(self.message('批准 '+command)))
  self.router.handle(self.message('批准 '+command));self.assertEqual(len(self.rpc.sent),1)
 def test_confirm_expiry_and_resolved(self):
  command=self.view();self.now+=121
  self.assertIn('无效',self.router.handle(self.message('批准 '+command)));self.assertFalse(self.rpc.sent)
  command=self.view();self.a.resolve(self.rpc,1)
  self.assertIn('resolved',self.router.handle(self.message('批准 '+command)));self.assertFalse(self.rpc.sent)
 def test_duplicate_message_ignored(self):
  m=self.message('状态');self.assertIsNotNone(self.router.handle(m));self.assertIsNone(self.router.handle(m))
 def test_long_details_cannot_confirm(self):
  self.a.items[self.token]['request']['params']['command']='x'*6000
  self.assertIn('过长',self.router.handle(self.message('查看 '+self.token)));self.assertFalse(self.router.challenges)
if __name__=='__main__':unittest.main()
