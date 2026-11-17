"""Read-only counts and status inspection for the explicitly created test task."""
import json,sqlite3,collections,re
from pathlib import Path
ROOT=Path(__file__).resolve().parent;TARGET='01a1d501-dfa9-7020-b93d-399281b3f0f7'
for name in ['replies.sqlite3','task-control.sqlite3']:
 db=sqlite3.connect((ROOT/name).as_uri()+'?mode=ro',uri=True)
 if name.startswith('replies'):
  print(json.dumps({'reply_counts':db.execute('SELECT kind,COUNT(*) FROM replies WHERE thread=? GROUP BY kind',(TARGET,)).fetchall()}))
  tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
  if 'task_events' in tables:print(json.dumps({'recent_events':db.execute('SELECT method,status FROM task_events WHERE thread=? ORDER BY received DESC LIMIT 8',(TARGET,)).fetchall()}))
 else:print(json.dumps({'sent_operations':db.execute('SELECT action,status,COUNT(*) FROM operations WHERE thread=? GROUP BY action,status',(TARGET,)).fetchall()}))
 db.close()
session=Path.home()/'.codex/sessions/2026/11/13/rollout-2026-11-13T21-03-52-01a1d501-dfa9-7020-b93d-399281b3f0f7.jsonl'
if session.exists():
 counts=collections.Counter();recent=[];settings=[]
 with session.open(encoding='utf-8') as file:
  for line in file:
   event=json.loads(line);p=event.get('payload',{});kind=p.get('type');counts[str(kind)]+=1
   if kind=='thread_settings_applied':settings.append({k:p.get(k) for k in ['model','model_provider','modelProvider','reasoning_effort']})
   if kind in ['error','warning','task_complete','task_started','agent_message']:
    message=str(p.get('message',''))[:700];message=re.sub(r'sk-[A-Za-z0-9_-]+','[redacted]',message)
    recent.append(dict(type=kind,time=event.get('timestamp'),message=message))
 print(json.dumps({'settings':settings,'rollout_event_counts':dict(counts),'recent_runtime_events':recent[-8:]},ensure_ascii=False))
