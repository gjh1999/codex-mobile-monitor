"""Read local Claude Code transcripts without loading credentials or changing sessions."""
import ctypes
import json
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path


def valid_session(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, TypeError, AttributeError):
        return False


def process_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == 'nt':
            from ctypes import wintypes
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return kernel.GetLastError() == 5  # Access denied is not proof of exit.
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ValueError, OSError, TypeError):
        return False


def timestamp(value, default=0):
    try:
        if isinstance(value, (int, float)):
            return value / 1000 if value > 1e12 else value
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError, TypeError):
        return default


class ClaudeSessions:
    def __init__(self, home=None, alive=process_alive):
        self.home = Path(home or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        self.alive = alive
        self.lock = threading.RLock()
        self.cache = {}

    def live_metadata(self, exclude_pids=()):
        sessions = {}
        for path in (self.home / 'sessions').glob('*.json'):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                if item.get('pid') in exclude_pids or not valid_session(item.get('sessionId')) or not self.alive(item.get('pid')):
                    continue
                tid = item['sessionId']
                details = {k: item.get(k) for k in ('status', 'waitingFor', 'updatedAt', 'statusUpdatedAt')}
                previous = sessions.get(tid)
                if previous is None or timestamp(details.get('updatedAt')) >= timestamp(previous.get('updatedAt')):
                    sessions[tid] = details
            except (OSError, ValueError, TypeError):
                continue
        return sessions

    def live(self, exclude_pids=()):
        return set(self.live_metadata(exclude_pids))

    @staticmethod
    def live_status(metadata):
        status = str(metadata.get('status') or '').strip().lower()
        if status == 'waiting':
            waiting = str(metadata.get('waitingFor') or '').strip().lower()
            if waiting in ('input needed', 'input', 'user input'):
                return {'type': 'active', 'activeFlags': ['waitingOnUserInput']}
            if waiting in ('approval needed', 'approval', 'permission', 'permission needed', 'permission approval'):
                return {'type': 'active', 'activeFlags': ['waitingOnApproval']}
            return {'type': 'unknown'}
        return {'type': {'busy': 'active', 'idle': 'idle'}.get(status, 'unknown')}


    def refresh(self):
        with self.lock:
            paths = {p.stem: p for p in (self.home / 'projects').glob('*/*.jsonl') if valid_session(p.stem)}
            for tid in set(self.cache) - paths.keys():
                self.cache.pop(tid, None)
            for tid, path in paths.items():
                try:
                    stat = path.stat()
                    entry = self.cache.get(tid)
                    if not entry or entry['path'] != path or stat.st_size < entry['offset'] or (stat.st_mtime_ns != entry['mtime'] and stat.st_size == entry['offset']):
                        entry = dict(path=path, offset=0, mtime=0, size=-1, messages=deque(maxlen=50), events=deque(maxlen=12),
                                     title='', cwd='', activity='unknown', updatedAt=stat.st_mtime)
                        self.cache[tid] = entry
                    if stat.st_mtime_ns == entry['mtime'] and stat.st_size == entry['size']:
                        continue
                    with path.open('rb') as stream:
                        stream.seek(entry['offset'])
                        while True:
                            start = stream.tell()
                            line = stream.readline()
                            if not line or not line.endswith(b'\n'):
                                entry['offset'] = start
                                break
                            entry['offset'] = stream.tell()
                            try:
                                event = json.loads(line)
                            except (ValueError, UnicodeDecodeError):
                                continue
                            self._event(entry, event)
                    entry['mtime'] = stat.st_mtime_ns
                    entry['size'] = stat.st_size
                except OSError:
                    continue

    @staticmethod
    def _event(entry, event):
        if event.get('isSidechain'):
            return
        kind = event.get('type')
        if event.get('cwd'):
            entry['cwd'] = event['cwd']
        if event.get('customTitle'):
            entry['title'] = event['customTitle'][:120]
        if kind not in ('assistant', 'user', 'system', 'progress'):
            return
        stamp = timestamp(event.get('timestamp'), entry['updatedAt'])
        entry['updatedAt'] = max(entry['updatedAt'], stamp)
        message = event.get('message') or {}
        blocks = message.get('content', [])
        if isinstance(blocks, str):
            text = blocks
            blocks = []
        else:
            text = '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text')
        if kind in ('assistant', 'user') and text and not event.get('isMeta'):
            role = 'assistant' if kind == 'assistant' else 'user'
            entry['messages'].append(dict(role=role, text=text[:20000], time=stamp, truncated=len(text) > 20000, id=event.get('uuid')))
            if kind == 'user' and not entry['title']:
                entry['title'] = ' '.join(text.split())[:80]
        tools = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_use']
        if kind == 'user' and (text or blocks):
            entry['activity'] = 'active'
        if tools:
            entry['activity'] = 'active'
            entry['events'].append(dict(method='Claude · ' + ', '.join(b.get('name', 'tool') for b in tools), status='执行工具', time=stamp))
        if kind == 'assistant' and message.get('stop_reason') in ('end_turn', 'stop_sequence', 'max_tokens', 'refusal'):
            entry['activity'] = 'idle'
        if kind == 'system' and event.get('subtype') == 'turn_duration':
            entry['activity'] = 'idle'
        if event.get('isApiErrorMessage'):
            entry['activity'] = 'systemError'
            entry['events'].append(dict(method='error', status={'error': text[:1500]}, time=stamp))

    def threads(self, limit=50):
        self.refresh()
        live = self.live_metadata()
        with self.lock:
            result = []
            for tid, entry in self.cache.items():
                status = entry['activity']
                updated = entry['updatedAt']
                if tid in live:
                    details = live[tid]
                    updated = max(updated, timestamp(details.get('updatedAt')))
                    current = self.live_status(details)
                    if not details.get('status'):
                        current = {'type': status if status != 'active' or time.time() - updated <= 120 else 'unknown'}
                else:
                    current = {'type': 'systemError' if status == 'systemError' else 'idle'}
                result.append(dict(id=tid, title=entry['title'] or '未命名 Claude 任务', cwd=entry['cwd'],
                                   updatedAt=updated, status=current, client='claude', source='Claude Code',
                                   externalOpen=tid in live))
            return sorted(result, key=lambda t: t['updatedAt'], reverse=True)[:limit]

    def thread(self, tid):
        if not valid_session(tid):
            return None
        return next((t for t in self.threads(limit=len(self.cache) + 1000) if t['id'] == tid), None)

    def conversation(self, tid):
        if not valid_session(tid):
            return {'messages': [], 'events': []}
        self.refresh()
        with self.lock:
            entry = self.cache.get(tid)
            return dict(messages=list(entry['messages']), events=list(reversed(entry['events']))) if entry else {'messages': [], 'events': []}
