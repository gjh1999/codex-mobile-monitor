"""Approximate labels from session metadata only; no conversation text is read."""
import json,os
from pathlib import Path
KNOWN={'01a1cad9-8028-7771-bf25-2115a5c73e1f':'医学影像顶会研究','01a1d43b-7a50-7612-8bd4-b6dd122b9c70':'Codex小时守护与Windows回复通知','01a1d501-dfa9-7020-b93d-399281b3f0f7':'桌面功能测试'}
def load_labels():
 labels={};root=Path(os.environ.get('USERPROFILE',Path.home()))/'.codex/sessions'
 if not root.exists():return labels
 for file in root.rglob('*.jsonl'):
  try:
   with file.open(encoding='utf-8') as stream:p=json.loads(stream.readline()).get('payload',{})
   sid=p.get('id') or p.get('session_id')
   if not sid:continue
   cwd=Path(p.get('cwd','')).name or '未知工作区';source=p.get('source') or p.get('originator') or '未知来源';created=str(p.get('timestamp',''))[:10]
   labels[sid]=dict(title=KNOWN.get(sid,f'历史任务 · {cwd} · {source} · {created}'),source=source,cwd=p.get('cwd',''))
  except (OSError,UnicodeError,ValueError,TypeError):continue
 return labels
