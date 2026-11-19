"""Read the existing journal for an authenticated UI; no inference or retries."""
import json,sqlite3


MESSAGE_LIMIT = 50
TEXT_LIMIT = 20000


def read_conversation(path,thread_id,include_ids=False,*,include_turns=False):
 if not path.exists():return {'messages':[],'events':[]}
 db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
 try:
  rows=db.execute('SELECT kind,text,received,item,turn FROM replies WHERE thread=? ORDER BY received DESC,rowid DESC LIMIT ?',(thread_id,MESSAGE_LIMIT)).fetchall()
  messages=[]
  for kind,text,stamp,item_id,turn_id in reversed(rows):
   if kind=='userMessage':
    try:
     content=json.loads(text)
     if isinstance(content,list):text='\n'.join(x.get('text','') for x in content if isinstance(x,dict))
    except ValueError:pass
   messages.append(dict(role='assistant' if kind=='agentMessage' else 'user',text=text[:TEXT_LIMIT],time=stamp,truncated=len(text)>TEXT_LIMIT))
   if include_ids:messages[-1]['id']=item_id
   if include_turns:messages[-1]['turnId']=turn_id
  events=[]
  tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  if 'task_events' in tables:
   for method,status,stamp in db.execute('SELECT method,status,received FROM task_events WHERE thread=? ORDER BY received DESC LIMIT 12',(thread_id,)):
    try:status=json.loads(status)
    except ValueError:pass
    events.append(dict(method=method,status=status,time=stamp))
  return {'messages':messages,'events':events}
 finally:db.close()
