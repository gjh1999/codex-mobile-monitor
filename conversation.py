"""Read the existing journal for an authenticated UI; no inference or retries."""
import json,sqlite3


def read_conversation(path,thread_id,include_ids=False):
 if not path.exists():return {'messages':[],'events':[]}
 db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
 try:
  rows=db.execute('SELECT kind,text,received,item FROM replies WHERE thread=? ORDER BY received DESC,rowid DESC LIMIT 50',(thread_id,)).fetchall()
  messages=[]
  for kind,text,stamp,item_id in reversed(rows):
   if kind=='userMessage':
    try:
     content=json.loads(text)
     if isinstance(content,list):text='\n'.join(x.get('text','') for x in content if isinstance(x,dict))
    except ValueError:pass
   messages.append(dict(role='assistant' if kind=='agentMessage' else 'user',text=text[:20000],time=stamp,truncated=len(text)>20000))
   if include_ids:messages[-1]['id']=item_id
  events=[]
  tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  if 'task_events' in tables:
   for method,status,stamp in db.execute('SELECT method,status,received FROM task_events WHERE thread=? ORDER BY received DESC LIMIT 12',(thread_id,)):
    try:status=json.loads(status)
    except ValueError:pass
    events.append(dict(method=method,status=status,time=stamp))
  return {'messages':messages,'events':events}
 finally:db.close()
