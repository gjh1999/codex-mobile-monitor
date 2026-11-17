"""Durable, deduplicated message journal. Never persist arbitrary RPC payloads."""
import json, sqlite3, threading, time,re

class ReplyLog:
 def __init__(self,path):
  self.lock=threading.Lock()
  self.db=sqlite3.connect(path,check_same_thread=False)
  self.db.execute('CREATE TABLE IF NOT EXISTS replies(thread TEXT, turn TEXT, item TEXT, kind TEXT, text TEXT, received REAL, PRIMARY KEY(thread,turn,item))')
  self.db.commit()
  self.db.execute('CREATE TABLE IF NOT EXISTS task_events(thread TEXT, turn TEXT, method TEXT, status TEXT, received REAL)');self.db.commit()
 def record(self,event,owned):
  p=event.get('params',{});tid=p.get('threadId');item=p.get('item',{})
  if tid not in owned:return False
  if event.get('method') in ('turn/started','turn/completed','thread/status/changed','thread/goal/updated','error'):
   with self.lock:
    status=p.get('status',p.get('turn',{}).get('status',p.get('goal',{}).get('status')))
    error=p.get('error') or p.get('turn',{}).get('error')
    if error:
     if isinstance(error,dict):error={k:error.get(k) for k in ['message','code','codexErrorInfo']}
     safe=re.sub(r'sk-[A-Za-z0-9_-]+','[redacted]',json.dumps(error,ensure_ascii=False))[:1500]
     status={'status':status,'error':safe}
    self.db.execute('INSERT INTO task_events VALUES(?,?,?,?,?)',(tid,p.get('turnId',p.get('turn',{}).get('id')),event['method'],json.dumps(status),time.time()));self.db.commit()
  if event.get('method')!='item/completed':return False
  kind=item.get('type')
  if kind not in ('agentMessage','userMessage'):return False
  body=item.get('text') if kind=='agentMessage' else json.dumps(item.get('content',[]),ensure_ascii=False)
  if not isinstance(body,str) or not item.get('id') or not p.get('turnId'):return False
  with self.lock:
   cur=self.db.execute('INSERT OR IGNORE INTO replies VALUES(?,?,?,?,?,?)',(tid,p['turnId'],item['id'],kind,body,time.time()))
   self.db.commit();return bool(cur.rowcount)
