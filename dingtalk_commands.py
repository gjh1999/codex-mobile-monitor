"""Allowlisted DingTalk commands. Receives ONLY official Stream callbacks.

No arbitrary shell execution, task creation, or blanket approval command.
"""
import json, secrets, threading, time
class CommandRouter:
 def __init__(self,approvals,status,identities,clock=time.time):
  self.approvals=approvals;self.status=status;self.clock=clock;self.lock=threading.Lock();self.challenges={};self.seen={}
  self.identities={tuple(str(x.get(k,'')) for k in ('corpId','staffId','conversationId')) for x in identities if all(x.get(k) for k in ('corpId','staffId','conversationId'))}
 def handle(self,data):
  identity=tuple(str(data.get(k,'')) for k in ('senderCorpId','senderStaffId','conversationId'))
  if identity not in self.identities or data.get('conversationType')!='1':return None
  if data.get('msgtype')!='text' or not data.get('msgId'):return None
  content=data.get('text',{}).get('content','')
  if not isinstance(content,str) or len(content)>500:return None
  with self.lock:
   now=self.clock();self.seen={k:v for k,v in self.seen.items() if v>now};self.challenges={k:v for k,v in self.challenges.items() if v['expires']>now}
   event=(identity,str(data['msgId']))
   if event in self.seen:return None
   self.seen[event]=now+86400
   parts=content.strip().split()
   if not parts:return None
   if parts==['状态']:
    s=self.status()
    return json.dumps({k:s.get(k) for k in ('updated','connection','quotaStatus','quota','threads','schedule')},ensure_ascii=False)[:6000]
   if parts==['待审批']:
    pending=[{'token':a['token'],'method':a['method'],'expires':a['expires']} for a in self.approvals.public() if a['decision'] is None and a['expires']>now]
    return json.dumps(pending,ensure_ascii=False) if pending else '当前没有本服务可处理的审批。'
   if len(parts)==2 and parts[0]=='查看':
    item=next((a for a in self.approvals.public() if a['token']==parts[1] and a['decision'] is None and a['expires']>now),None)
    if not item:return '请求不存在或已失效。'
    details=json.dumps({'method':item['method'],'details':item['details']},ensure_ascii=False)
    if len(details)>5000:return '操作详情过长，不能在钉钉中批准。请在桌面软件中完整核对。'
    code=secrets.token_urlsafe(9)
    self.challenges[(identity,parts[1])]={'code':code,'expires':min(item['expires'],now+120)}
    return details+'\n单次确认（120秒内）：批准 '+parts[1]+' '+code+'\n或：拒绝 '+parts[1]
   if len(parts)==3 and parts[0]=='批准':
    challenge=self.challenges.pop((identity,parts[1]),None)
    if not challenge or not secrets.compare_digest(challenge['code'],parts[2]):return '确认码无效。请重新查看请求。'
    try:return '审批结果：'+self.approvals.decide(parts[1],True)
    except (KeyError,ValueError):return '请求失效或缺少具体操作详情，未批准。'
   if len(parts)==2 and parts[0]=='拒绝':
    try:return '审批结果：'+self.approvals.decide(parts[1],False)
    except KeyError:return '请求不存在或已失效。'
   return '可用指令：状态、待审批、查看 <请求token>、批准 <请求token> <确认码>、拒绝 <请求token>。仅处理本服务连接上的请求。'

def run_stream(router,client_id,client_secret,on_connected=lambda:None):
 # Optional dependency is imported only when explicitly configured.
 import dingtalk_stream
 class Handler(dingtalk_stream.ChatbotHandler):
  async def process(self,callback):
   on_connected()
   response=router.handle(callback.data)
   if response is not None:
    incoming=dingtalk_stream.ChatbotMessage.from_dict(callback.data)
    self.reply_text(response,incoming)
   return dingtalk_stream.AckMessage.STATUS_OK,'OK'
 client=dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(client_id,client_secret))
 client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC,Handler())
 client.start_forever()
