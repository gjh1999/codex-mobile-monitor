"""Opt-in service-created tasks; never adopts a desktop thread ID."""
import hashlib,json,sqlite3
from pathlib import Path
class ManagedTasks:
 def __init__(self,path,workspace):
  self.workspace=Path(workspace).resolve();self.db=sqlite3.connect(path)
  self.db.execute('CREATE TABLE IF NOT EXISTS managed(name TEXT PRIMARY KEY, fingerprint TEXT, thread TEXT, phase TEXT)');self.db.commit()
 def sync(self,rpc,jobs):
  tasks=[];status=[]
  for job in jobs:
   created=False
   name=job.get('name','')
   if not job.get('enabled') or not job.get('exclusiveOwner'):status.append('disabled:'+name);continue
   try:
    if not name or not job.get('objective') or not job.get('prompt'):raise ValueError()
    cwd=(self.workspace/job.get('cwd','.')).resolve()
    if not cwd.is_dir() or not cwd.is_relative_to(self.workspace):raise ValueError()
    digest=hashlib.sha256(json.dumps({'cwd':str(cwd),'objective':job['objective'],'prompt':job['prompt']},sort_keys=True).encode()).hexdigest()
    row=self.db.execute('SELECT fingerprint,thread,phase FROM managed WHERE name=?',(name,)).fetchone()
    if row and row[0]!=digest:status.append('blocked:config_changed:'+name);continue
    if not row:
     # Crash after reservation cannot create a duplicate remote thread on restart.
     self.db.execute('INSERT INTO managed VALUES(?,?,?,?)',(name,digest,None,'creating'));self.db.commit()
     result=rpc.call('thread/start',{'cwd':str(cwd),'approvalPolicy':'on-request','sandbox':'workspace-write'})
     tid=result['thread']['id']
     self.db.execute('UPDATE managed SET thread=?,phase=? WHERE name=?',(tid,'goal_pending',name));self.db.commit()
     row=(digest,tid,'goal_pending')
     created=True
    if not row[1]:status.append('blocked:uncertain_creation:'+name);continue
    tid=row[1]
    if tid not in rpc.owned_threads:
     if not created:rpc.call('thread/resume',{'threadId':tid})
     if row[2]=='goal_pending':
      goal=rpc.call('thread/goal/get',{'threadId':tid}).get('goal')
      if goal and goal.get('objective')!=job['objective']:status.append('blocked:goal_mismatch:'+name);continue
      if not goal:rpc.call('thread/goal/set',{'threadId':tid,'objective':job['objective'],'status':'active','origin':'user'})
      self.db.execute('UPDATE managed SET phase=? WHERE name=?',('ready',name));self.db.commit()
     rpc.owned_threads.add(tid)
    tasks.append({'threadId':tid,'prompt':job['prompt'],'interval':job.get('interval',3600),'enabled':True,'exclusiveOwner':True})
    status.append('managed:'+name)
   except Exception:status.append('unavailable:'+name)
  return tasks,status
