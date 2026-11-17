import json,sys
from automation import Approvals
from rpc_client import RPC

target=sys.argv[1] if len(sys.argv)>1 else '01a1cad9-8028-7771-bf25-2115a5c73e1f'
rpc=RPC(Approvals())
try:
 print(json.dumps(rpc.call('thread/goal/get',{'threadId':target}).get('goal'),ensure_ascii=False,indent=2))
finally:
 rpc.close()
