"""Fail-closed exact approval rules and native notifications."""
import hashlib,json,subprocess,sqlite3,time,sys
from pathlib import Path

class ScopedPolicy:
 def __init__(self,workspace,rules=()):self.root=Path(workspace).resolve();self.rules=rules
 def allows(self,rpc,request):
  p=request.get('params',{});tid=p.get('threadId')
  if tid not in rpc.owned_threads:return False
  # Permission expansion and file changes without concrete, verifiable payloads remain manual.
  if request.get('method')!='item/commandExecution/requestApproval':return False
  if p.get('kind','command')!='command' or p.get('networkApprovalContext') is not None:return False
  command=p.get('command');cwd=p.get('cwd')
  if not isinstance(command,str) or not cwd:return False
  directory=Path(cwd).resolve()
  if not directory.is_relative_to(self.root):return False
  # No shell composition, network, policy or administration even if accidentally listed.
  if any(c in command for c in (';','|','&','`','$','\n','\r','>','<')):return False
  if any(c in command.lower() for c in ('powershell','cmd.exe','curl','wget','ssh','sudo','registry','executionpolicy')):return False
  return any(r.get('threadId')==tid and r.get('command')==command and Path(r.get('cwd','')).resolve()==directory for r in self.rules)

class WindowsNotifications:
 def __init__(self,path,sender=None):
  self.db=sqlite3.connect(path);self.db.execute('CREATE TABLE IF NOT EXISTS native_events(key TEXT PRIMARY KEY, stamp REAL, result TEXT)');self.db.commit()
  self.sender=sender or self.send
 def send(self,title,body):
  if getattr(sys,'frozen',False):
   base=Path(sys.executable).resolve().parent
   script=base/'windows_notify.ps1'
   if not script.exists():script=Path(getattr(sys,'_MEIPASS',base))/'windows_notify.ps1'
  else:script=Path(__file__).resolve().parent/'windows_notify.ps1'
  result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-File',str(script),'-Title',title,'-Body',body],capture_output=True,text=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  return result.stdout.strip() if result.returncode==0 else 'api_failed'
 def notify(self,key,title,body):
  cur=self.db.execute('INSERT OR IGNORE INTO native_events VALUES(?,?,?)',(key,time.time(),'reserved'));self.db.commit()
  if not cur.rowcount:return 'duplicate'
  try:result=self.sender(title,body[:220])
  except Exception:result='api_failed_or_uncertain'
  self.db.execute('UPDATE native_events SET result=? WHERE key=?',(result,key));self.db.commit();return result
