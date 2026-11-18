"""In-process desktop workers. No task ownership is inferred from configuration."""
import json, os, sqlite3, threading, time, sys
from conversation import read_conversation
from pathlib import Path
from automation import Approvals, Scheduler
from channels_dingtalk import DingTalkWebhook
from rpc_client import RPC
from managed_tasks import ManagedTasks
from reply_log import ReplyLog
from daemon_support import ScopedPolicy,WindowsNotifications
from attachments import Attachments
from task_control import TaskControl
from history_labels import load_labels
from coordination_client import device_client
def _runtime_root():
 # A frozen app must keep its databases/configuration beside the EXE.  PyInstaller's
 # temporary extraction directory is disposable and cannot be used for state.
 if getattr(sys, 'frozen', False):
  return Path(sys.executable).resolve().parent
 return Path(__file__).resolve().parent

ROOT=_runtime_root()
approvals=Approvals()
state={'serviceVersion':'0.7.3-desktop-only','pid':os.getpid(),'updated':None,'quota':None,'quotaStatus':'unknown','threads':[],'schedule':[], 'connection':'starting','notification':'not_configured','wake':{'mode':'guarded','reason':'Only connection-owned threads with active goals are eligible.'},'claude':{'connection':'starting','threads':[],'controls':{'tasks':[],'operations':[]},'quotaStatus':'unknown','quota':None,'schedule':[]}}
state_lock=threading.Lock()
controller=None
claude_controller=None
claude_rpc=None
claude_journal=None
history_labels={}

def client_snapshot(snap,client='codex'):
 if client not in ('codex','claude'):raise ValueError('无效客户端。')
 if client=='codex':
  result={k:v for k,v in snap.items() if k!='claude'}
 else:
  result=dict(snap.get('claude') or {'connection':'starting','threads':[],'controls':{'tasks':[],'operations':[]},'quota':None,'quotaStatus':'unknown','schedule':[]})
 result['client']=client
 result['approvals']=[a for a in approvals.public() if a.get('client','codex')==client]
 return result

def get_controller(client='codex'):
 if client not in ('codex','claude'):raise ValueError('无效客户端。')
 return claude_controller if client=='claude' else controller

def conversation_for(thread_id,client='codex'):
 if client not in ('codex','claude'):raise ValueError('无效客户端。')
 if client=='claude':
  return claude_rpc.conversation(thread_id) if claude_rpc else {'messages':[],'events':[]}
 return read_conversation(ROOT/'replies.sqlite3',thread_id)
def snapshot():
 with state_lock:result=json.loads(json.dumps(state))
 # Control intent and fast interrupts can change between polling rounds. Read the
 # short, DB-only snapshot so a slow RPC never leaves Pause/Send falsely enabled.
 for client,target in (('codex',controller),('claude',claude_controller)):
  if target is None or not hasattr(target,'snapshot'):continue
  try:controls=target.snapshot()
  except sqlite3.Error:continue
  view=result if client=='codex' else result.setdefault('claude',{})
  view['controls']=controls;view['attachments']=controls['tasks']
 return result
def update(**values):
 with state_lock:state.update(values)
def config():
 path=ROOT/'local-config.json'
 return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
def clean_limits(x):
 def window(w):return {k:w.get(k) for k in ('usedPercent','windowDurationMins','resetsAt')} if isinstance(w,dict) else None
 def one(v):return {**{key:v.get(key) for key in ('spendControlReached','rateLimitReachedType','individualLimit')},'primary':window(v.get('primary')),'secondary':window(v.get('secondary'))}
 return {'ordinaryUsageAllowed':x.get('ordinaryUsageAllowed'),'default':one(x.get('rateLimits') or {}),'models':{k:one(v) for k,v in (x.get('rateLimitsByLimitId') or {}).items()}}
def collect(rpc,cfg,scheduler):
 approvals.expire()
 # Read current backend quota before any scheduler decision. Reset times are only
 # wake-up hints; ordinaryUsageAllowed must explicitly confirm recovery.
 quota=None
 try:
  quota=rpc.call('account/rateLimits/read',{})
  update(quota=clean_limits(quota),quotaStatus='official_api')
 except Exception:update(quota=None,quotaStatus='unknown: endpoint unavailable')
 schedule_result=scheduler.tick(rpc,cfg.get('tasks',[]),quota=quota)
 update(quotaWaits=scheduler.quota_recovery.snapshot())
 if schedule_result:update(schedule=schedule_result,lastScheduleCheck=scheduler.last_check)
 try:
  data=rpc.call('thread/list',{'limit':20})
  names=((snapshot().get('controls') or {}).get('names') or {})
  update(threads=[{'id':t['id'],'title':names.get(t['id']) or t.get('name') or t.get('title') or history_labels.get(t['id'],{}).get('title') or '未命名历史任务','source':history_labels.get(t['id'],{}).get('source'),'cwd':t.get('cwd') or history_labels.get(t['id'],{}).get('cwd'),'status':t.get('status',{'type':'unknown'}),'updatedAt':t.get('updatedAt')} for t in data.get('data',[])])
 except Exception:update(threads=[],threadStatus='unavailable')
 update(updated=int(time.time()),connection='disconnected' if rpc.closed else 'connected')
class Notifications:
 def __init__(self,path):
  self.db=sqlite3.connect(path)
  self.db.execute('CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,status TEXT)');self.db.commit()
 def deliver(self,channel,event,title,body):
  # Reserve before POST. Uncertain delivery is retained, never blindly retried.
  cur=self.db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?)',(event,'reserved'));self.db.commit()
  if not cur.rowcount:return 'duplicate'
  result=channel.send(event,title,body)['status']
  self.db.execute('UPDATE deliveries SET status=? WHERE id=?',(result,event));self.db.commit()
  return result

def poll(stop):
 global controller,history_labels
 scheduler=Scheduler(str(ROOT/'scheduler.sqlite3'),approvals)
 notifications=Notifications(str(ROOT/'notifications.sqlite3'))
 managed=ManagedTasks(str(ROOT/'managed.sqlite3'),ROOT.parent)
 rpc=None;channel=None;failures=0;drained=[]
 controller=TaskControl(ROOT/'task-control.sqlite3');controller.seed(config().get('autoAttachThreads',[]))
 journal=ReplyLog(str(ROOT/'replies.sqlite3'))
 native=WindowsNotifications(str(ROOT/'windows-notifications.sqlite3'))
 try:
  while not stop.is_set():
   try:
    cfg=config()
    for previous in drained[:]:
     previous.project_writes.release_closed()
     if not previous.project_writes.tickets:drained.remove(previous)
    if not history_labels:history_labels=load_labels()
    if rpc is None or rpc.closed:
     if rpc:
      rpc.close()
      if getattr(rpc,'draining',False):
       rpc.p.wait(timeout=3)
       drained.append(rpc)
     client=device_client(cfg)
     rpc=RPC(approvals,journal=journal,coordination=client) if client else RPC(approvals,journal=journal)
    if getattr(rpc,'project_writes',None) and rpc.reconcile_writes():continue
    controller.tick(rpc)
    controls=controller.snapshot();update(controls=controls,attachments=controls['tasks'])
    managed_jobs,managed_status=managed.sync(rpc,cfg.get('managedTasks',[]))
    update(managedTasks=managed_status)
    automatic_tasks=controller.automatic_tasks(rpc,cfg.get('tasks',[])+managed_jobs+controller.scheduled_tasks(rpc))
    collect(rpc,{**cfg,'tasks':automatic_tasks},scheduler)
    policy=ScopedPolicy(ROOT.parent,cfg.get('autoApprovalRules',[]))
    for token,value in list(approvals.items.items()):
     if value['decision'] is None and getattr(value['rpc'],'client_type','codex')=='codex':
      if value['rpc'] is rpc and policy.allows(rpc,value['request']):approvals.decide(token,True)
      else:
       details=value['request'].get('params',{})
       operation=str(details.get('command') or details.get('reason') or value['request'].get('method','权限请求'))
       update(windowsNotification=native.notify('approval:'+token,'Codex：需要你审批',' '.join(operation.split())[:160]+' | 请在桌面软件中核对详情后批准或拒绝'))
    with journal.lock:
     replies=journal.db.execute("SELECT thread,turn,item,text FROM replies WHERE kind='agentMessage'").fetchall()
    for tid,turn,item,reply in replies:
     excerpt=' '.join(reply.split())[:160]
     update(windowsNotification=native.notify('reply:'+tid+':'+turn+':'+item,'Codex 回复',excerpt+' | 完整回复保存在 replies.sqlite3'))
    for result in snapshot().get('schedule',[]):
     if result.startswith(('backoff:','blocked:goal')):
      native.notify(result+':'+str(int(time.time()//3600)),'Codex task waiting',result)
    webhook=os.environ.get('DINGTALK_WEBHOOK');secret=os.environ.get('DINGTALK_SIGNING_SECRET')
    if webhook and secret:
     if channel is None:channel=DingTalkWebhook(webhook,secret)
     update(notification='configured')
     for a in approvals.public():
      if a['decision'] is None:
       result=notifications.deliver(channel,a['token'],'需要人工审批','请在桌面软件中查看具体操作。当前通知不接受文字回复批准。')
       update(notification=result)
    failures=0
   except Exception:
    failures+=1;update(serviceStatus='retrying; details withheld',updated=int(time.time()))
    native.notify('failure:'+str(int(time.time()//3600)),'Codex monitor retrying','Connection unavailable; no replacement research thread created.')
   stop.wait(min(300,3*2**min(failures,6)))
 finally:
  if rpc:
   if getattr(rpc,'project_writes',None) and not rpc.closed:rpc.reconcile_writes(shutdown=True)
   rpc.close()
  if controller:controller.db.close()
  scheduler.db.close();notifications.db.close();managed.db.close()

def poll_claude(stop):
 """An independent worker keeps Codex outages from blocking Claude monitoring."""
 global claude_controller,claude_rpc,claude_journal
 from claude_rpc import ClaudeCodeRPC
 claude_controller=TaskControl(ROOT/'claude-task-control.sqlite3')
 claude_controller.seed(config().get('claudeAutoAttachThreads',[]),managed=False)
 claude_journal=ReplyLog(str(ROOT/'claude-replies.sqlite3'))
 native=WindowsNotifications(str(ROOT/'claude-windows-notifications.sqlite3'))
 try:
  while not stop.is_set():
   try:
    if claude_rpc is None or claude_rpc.closed:
     claude_rpc=ClaudeCodeRPC(approvals=approvals,journal=claude_journal,drafts_path=ROOT/'claude-drafts.json')
    claude_controller.tick(claude_rpc)
    schedule=claude_controller.tick_auto_progress(claude_rpc)
    controls=claude_controller.snapshot()
    threads=claude_rpc.call('thread/list',{'limit':50}).get('data',[])
    names=controls.get('names') or {}
    for item in threads:
     if item.get('id') in names:item['title']=names[item['id']]
    approvals.expire()
    for a in approvals.public():
     if a.get('client')=='claude' and a['decision'] is None:
      native.notify('approval:'+a['token'],'Claude Code：需要你审批',a['details'].get('reason','请在监控中查看工具申请。'))
    with claude_journal.lock:
     replies=claude_journal.db.execute("SELECT thread,turn,item,text FROM replies WHERE kind='agentMessage'").fetchall()
    for tid,turn,item,reply in replies:
     native.notify('reply:'+tid+':'+turn+':'+item,'Claude Code 回复',' '.join(reply.split())[:160])
    update(claude={'connection':'connected','updated':int(time.time()),'threads':threads,'controls':controls,'attachments':controls['tasks'],
                   'quota':None,'quotaStatus':'unknown: Claude Code CLI does not expose official quota','schedule':schedule,
                   'capabilities':{'automaticContinue':True},'serviceStatus':'','notification':'local'})
   except Exception as error:
    update(claude={'connection':'disconnected','updated':int(time.time()),'threads':[],
                   'controls':claude_controller.snapshot(),'quota':None,'quotaStatus':'unknown','schedule':[],
                   'serviceStatus':str(error)[:300] if isinstance(error,FileNotFoundError) else 'Claude Code 连接待检查，请查看本机 CLI。'})
   stop.wait(3)
 finally:
  if claude_rpc:claude_rpc.close()
  claude_controller.db.close();claude_journal.db.close();native.db.close()

class ServiceWorkers:
 def __init__(self,threads,service_lock=None):self.threads=threads;self.service_lock=service_lock
 def join(self,timeout=None):
  deadline=None if timeout is None else time.monotonic()+timeout
  for thread in self.threads:thread.join(None if deadline is None else max(0,deadline-time.monotonic()))
  if not self.is_alive() and self.service_lock:self.service_lock.close()
 def is_alive(self):return any(thread.is_alive() for thread in self.threads)

def start_background_service(stop=None, embed_web=None):
 """Start independent Codex and Claude workers inside the desktop process."""
 from service_lock import ServiceLock
 stop = stop or threading.Event()
 if embed_web is None:embed_web=os.environ.get('MONITOR_EMBED_WEB')=='1'
 web_worker=None
 if embed_web:
  from web_server import embedded_web_worker
  web_worker=embedded_web_worker(stop)
 service_lock=ServiceLock(ROOT/'monitor.lockfile')
 worker=threading.Thread(target=poll,args=(stop,),daemon=True,name='monitor-worker')
 claude_worker=threading.Thread(target=poll_claude,args=(stop,),daemon=True,name='claude-monitor-worker')
 worker.start();claude_worker.start()
 if web_worker:web_worker.start()
 if os.environ.get('DINGTALK_CLIENT_ID') and os.environ.get('DINGTALK_CLIENT_SECRET'):
  from dingtalk_commands import CommandRouter,run_stream
  cfg=config()
  router=CommandRouter(approvals,snapshot,cfg.get('streamIdentities',[]))
  def stream_worker():
   update(streamStatus='starting; awaiting callback')
   try:run_stream(router,os.environ['DINGTALK_CLIENT_ID'],os.environ['DINGTALK_CLIENT_SECRET'],lambda:update(streamStatus='callback_received'))
   except Exception:update(streamStatus='unavailable; credentials or SDK need local verification')
  threading.Thread(target=stream_worker,daemon=True,name='dingtalk-stream').start()
 return stop,ServiceWorkers([worker,claude_worker]+([web_worker] if web_worker else []),service_lock)
