"""Approximate labels from session metadata only; no conversation text is read."""
import json,os
from pathlib import Path
KNOWN={'01a1cad9-8028-7771-bf25-2115a5c73e1f':'医学影像顶会研究','01a1d43b-7a50-7612-8bd4-b6dd122b9c70':'Codex小时守护与Windows回复通知','01a1d501-dfa9-7020-b93d-399281b3f0f7':'桌面功能测试'}

def read_label(file):
 try:
  with file.open(encoding='utf-8') as stream:p=json.loads(stream.readline(262144)).get('payload',{})
  sid=p.get('id') or p.get('session_id')
  if not isinstance(sid,str) or not sid:return None
  directory=p.get('cwd','')
  if not isinstance(directory,str):return None
  cwd=Path(directory).name or '未知工作区';source=p.get('source') or p.get('originator') or '未知来源';created=str(p.get('timestamp',''))[:10]
  return sid,dict(title=KNOWN.get(sid,f'历史任务 · {cwd} · {source} · {created}'),source=source,cwd=directory)
 except (OSError,ValueError,TypeError,AttributeError):return None

def load_labels():
 labels={};root=Path(os.environ.get('CODEX_HOME') or Path(os.environ.get('USERPROFILE',Path.home()))/'.codex')/'sessions'
 if not root.exists():return labels
 for file in root.rglob('*.jsonl'):
  entry=read_label(file)
  if entry:
   sid,label=entry
   labels[sid]=label
 return labels
