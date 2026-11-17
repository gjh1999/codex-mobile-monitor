"""Load explicitly selected existing tasks automatically; never starts a turn."""
import re,time


class Attachments:
 def __init__(self,clock=time.time):self.clock=clock;self.records={};self.connection=None
 def sync(self,rpc,targets):
  if rpc is not self.connection:self.connection=rpc;self.records={}
  result=[]
  for target in targets:
   if not isinstance(target,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',target):continue
   old=self.records.get(target)
   if old and self.clock()<old['retryAt']:result.append(old);continue
   record={'threadId':target,'status':'connecting','updatedAt':self.clock()}
   try:
    thread=rpc.call('thread/resume',{'threadId':target,'excludeTurns':True}).get('thread',{})
    if thread.get('id')!=target:raise ValueError('Unexpected thread identity')
    record.update(status='attached',threadStatus=thread.get('status'),title=thread.get('name') or thread.get('title'),
                  message='任务已载入当前连接。自动续跑控制权仍需独立核验。',automaticContinue=False)
    try:record['goalStatus']=(rpc.call('thread/goal/get',{'threadId':target}).get('goal') or {}).get('status')
    except Exception:record['goalStatus']='unknown'
    record['retryAt']=self.clock()+3600
   except Exception as error:
    detail=getattr(error,'detail','') or ''
    reason='接口未找到任务历史，当前连接无法载入。' if 'no rollout found' in detail else '接口未找到该任务。' if 'not found' in detail else '后台连接暂时无法载入任务。'
    record.update(status='failed',message=reason,errorCode=getattr(error,'code',None),automaticContinue=False,retryAt=self.clock()+300)
   self.records[target]=record;result.append(record)
  return result
