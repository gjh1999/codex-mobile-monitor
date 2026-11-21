"""Read local Codex completions and watch approvals on its shared app-server."""

import json
import queue
import re
import time
from datetime import datetime
from pathlib import Path

from codex_history import LINE_LIMIT
from coordination_client import ControlDenied, PersonalTaskLocks, personal_project_at
from rpc_errors import RPCError
from universal_rpc import UniversalRPC


THREAD_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def completion(thread, turn, when, title=''):
    if not isinstance(thread, str) or not THREAD_ID.fullmatch(thread):
        return None
    if not isinstance(turn, str) or not turn or len(turn) > 100:
        return None
    return {'threadId': thread, 'turnId': turn, 'time': when, 'title': title[:120]}


def completion_from_rpc(params):
    turn = params.get('turn') or {}
    if turn.get('status') == 'completed':
        return completion(params.get('threadId'), turn.get('id'), time.time())
    return None


class RolloutWatch:
    def __init__(self, home, started):
        self.folder = Path(home) / 'sessions'
        self.started = started
        self.positions = {}

    def scan(self, stop):
        seen = set()
        for path in self.folder.rglob('rollout-*.jsonl'):
            if stop.is_set():
                return
            if path.is_symlink() or not THREAD_ID.fullmatch(path.stem[-36:]):
                continue
            seen.add(path)
            try:
                yield from self.read_new(path, stop)
            except OSError:
                continue
        self.positions = {path: value for path, value in self.positions.items() if path in seen}

    def read_new(self, path, stop):
        stat = path.stat()
        if stat.st_mtime < self.started:
            return
        identity = stat.st_ino, stat.st_dev
        cached, position, discarding, title = self.positions.get(path, (None, 0, False, ''))
        if cached != identity or stat.st_size < position:
            position, discarding, title = 0, False, ''
        if stat.st_size == position:
            return
        with path.open('rb') as file:
            if not position:
                header = file.readline(LINE_LIMIT + 1)
                if not header.endswith(b'\n'):
                    return
                try:
                    meta = json.loads(header)
                    if meta['type'] != 'session_meta' or meta['payload']['id'] != path.stem[-36:]:
                        return
                    title = Path(meta['payload'].get('cwd') or '').name
                except (ValueError, UnicodeError, KeyError, TypeError):
                    return
            else:
                file.seek(position)
            while not stop.is_set():
                position = file.tell()
                line = file.readline(LINE_LIMIT + 1)
                if not line:
                    break
                if not line.endswith(b'\n'):
                    if len(line) <= LINE_LIMIT:
                        break
                    discarding = True
                    continue
                if discarding or b'"task_complete"' not in line:
                    discarding = False
                    continue
                try:
                    event = json.loads(line)
                    if event['type'] != 'event_msg' or event['payload']['type'] != 'task_complete':
                        continue
                    stamp = datetime.fromisoformat(event['timestamp'])
                    if stamp.tzinfo is None or stamp.timestamp() < self.started:
                        continue
                    result = completion(path.stem[-36:], event['payload'].get('turn_id'), stamp.timestamp(), title)
                    if result:
                        yield result
                except (ValueError, UnicodeError, KeyError, TypeError, OverflowError):
                    continue
            self.positions[path] = identity, position, discarding, title


class SharedCodex(UniversalRPC):
    """Subscribe to waiting threads on an already running local daemon."""

    monitor_only = True
    client_type = 'codex'

    def __init__(self, approvals, report_completion, approval_coordination):
        self.approvals = approvals
        self.report_completion = report_completion
        self.approval_coordination = approval_coordination
        self.subscriptions = set()
        process = self._find_process('codex', arguments=['app-server', 'proxy'])
        super().__init__('codex', custom_process=process)

    def approval_guard(self, thread):
        try:
            client, personal = self.approval_coordination()
            if client is None or not (client.paths or personal):
                return None
            if not isinstance(thread, str) or not THREAD_ID.fullmatch(thread):
                raise ControlDenied(detail='无法核对审批所属的 Codex 会话。')
            result = self.call('thread/read', {'threadId': thread, 'includeTurns': False}, timeout=3)
            cwd = (result.get('thread') or {}).get('cwd')
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ControlDenied(detail='无法核对审批所属的项目目录。')
            mapped = client.project_at(cwd)
            registered = client.threads.get(thread)
            if registered and mapped and registered != mapped:
                raise ControlDenied(detail='会话与实际目录属于不同登记项目。')
            registered = registered or mapped
            if personal and not Path(cwd).is_dir():
                raise ControlDenied(detail='审批所属的本机项目目录不可用。')
            project = personal_project_at(cwd) if personal else None
        except ControlDenied:
            raise
        except (OSError, RPCError, queue.Empty, ValueError, TypeError, KeyError, AttributeError) as error:
            raise ControlDenied(detail='无法核对审批所属的项目控制权。') from error

        if not (project or registered):
            return None

        def guard():
            try:
                if project:
                    PersonalTaskLocks(client).require_approval_owner(thread, project)
                if registered:
                    state = client.request(registered, 'control')
                    writer = state['writer'] or {}
                    if (writer.get('device') != client.device or writer.get('version') != state['version']
                            or state['humans']):
                        raise ControlDenied(detail='本机已失去登记项目的写入权。')
            except ControlDenied:
                raise
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                raise ControlDenied(detail='无法复核审批所属的项目控制权。') from error
            return True

        return guard

    def poll_approvals(self, stop):
        loaded = set(self.call('thread/loaded/list', {}, timeout=3).get('data', []))
        for thread in self.subscriptions - loaded:
            self.approvals.dismiss_thread(self, thread)
        self.subscriptions.intersection_update(loaded)
        for thread in sorted(loaded):
            if stop.is_set():
                return
            params = {'threadId': thread}
            try:
                current = self.call('thread/read', {**params, 'includeTurns': False}, timeout=3).get('thread') or {}
                status = current.get('status') or {}
                if status.get('type') != 'active' or 'waitingOnApproval' not in status.get('activeFlags', []):
                    if thread in self.subscriptions:
                        self.call('thread/unsubscribe', params, timeout=3)
                        self.approvals.dismiss_thread(self, thread)
                        self.subscriptions.discard(thread)
                    continue
                if thread not in self.subscriptions:
                    resumed = self.call('thread/resume', {**params, 'excludeTurns': True}, timeout=3).get('thread') or {}
                    if (resumed.get('status') or {}).get('type') != 'active':
                        self.call('thread/unsubscribe', params, timeout=3)
                        self.approvals.dismiss_thread(self, thread)
                        continue
                    self.subscriptions.add(thread)
                self.approvals.refresh_live(self, thread)
            except RPCError:
                # A foreground client can close the thread during discovery.
                continue
