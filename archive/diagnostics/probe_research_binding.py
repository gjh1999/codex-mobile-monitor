"""Read-only binding check; never resumes, creates, or wakes a model task."""
import argparse,json,os
from pathlib import Path
from automation import Approvals
from rpc_client import RPC


def main():
 parser=argparse.ArgumentParser();parser.add_argument('--load-without-turn',action='store_true');parser.add_argument('--executable');args=parser.parse_args()
 rpc=RPC(Approvals(),executable=args.executable)
 target='01a1cad9-8028-7771-bf25-2115a5c73e1f'
 result={'connected':True,'owned_by_monitor_connection':target in rpc.owned_threads,'executable':str(rpc.p.args[0]),'codex_home_override':os.environ.get('CODEX_HOME'),'userprofile':os.environ.get('USERPROFILE')}
 try:
  try:
   listing=rpc.call('thread/list',{'limit':100,'modelProviders':[],'sourceKinds':[]})
   result['listed_count']=len(listing.get('data',[]))
   match=next((t for t in listing.get('data',[]) if t.get('id')==target),None)
   result['listed_record']=None if match is None else {k:match.get(k) for k in ['id','path','cwd','source','status']}
  except Exception as error:result['listing_error']=getattr(error,'detail',type(error).__name__)
  if args.load_without_turn:
   try:
    resumed=rpc.call('thread/resume',{'threadId':target},timeout=45)
    result['load_without_turn']={'id':resumed.get('thread',{}).get('id'),'status':resumed.get('thread',{}).get('status')}
   except Exception as error:result['load_without_turn']={'code':getattr(error,'code',None),'message':getattr(error,'detail',None)}
  for method,key in [('thread/read','thread_status'),('thread/goal/get','goal_status')]:
   try:
    value=rpc.call(method,{'threadId':target,'includeTurns':False} if method=='thread/read' else {'threadId':target})
    result[key]=value.get('thread',{}).get('status') if method=='thread/read' else (value.get('goal') or {}).get('status')
   except Exception as error:result[key]={'status':'unavailable','type':type(error).__name__,'code':getattr(error,'code',None),'message':getattr(error,'detail',None)}
 finally:rpc.close()
 session=Path(os.environ['USERPROFILE'])/'.codex/sessions/2026/11/11/rollout-2026-11-11T21-43-34-01a1cad9-8028-7771-bf25-2115a5c73e1f.jsonl'
 if session.exists():
  with session.open(encoding='utf-8') as file:meta=json.loads(file.readline()).get('payload',{})
  result['local_history']={k:meta.get(k) for k in ['id','cli_version','history_mode']}
 print(json.dumps(result))


if __name__=='__main__':main()
