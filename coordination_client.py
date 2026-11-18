"""Device-side project mapping, foreground activity, and Codex write reservations."""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from rpc_errors import RPCError


class ControlDenied(RPCError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ControlClient:
    def __init__(self, config, token):
        url = config['url'].rstrip('/')
        parts = urlsplit(url)
        loopback = parts.hostname in ('127.0.0.1', '::1', 'localhost')
        if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                or parts.scheme not in ('http', 'https') or (parts.scheme == 'http' and not loopback)):
            raise ValueError('协调地址需要 HTTPS 或本机回环 HTTP')
        self.url = url
        self.device = config['device']
        self.token = token
        self.opener = build_opener(NoRedirect(), *([ProxyHandler({})] if loopback else []))
        self.paths = {}
        self.threads = {}
        for project, mapping in config['projects'].items():
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', project):
                raise ValueError('项目标识应由英文、数字、连字符或下划线组成')
            path = Path(mapping['path'])
            if not path.is_absolute():
                raise ValueError('项目目录必须为绝对路径')
            path = path.resolve()
            if path in self.paths:
                raise ValueError('项目目录重复')
            self.paths[path] = project
            for thread in mapping.get('threads', []):
                if thread in self.threads:
                    raise ValueError('线程在项目配置中重复')
                self.threads[thread] = project

    def request(self, project, endpoint, body=None):
        if project not in self.paths.values():
            raise ControlDenied(detail='此设备尚未配置该项目')
        if not self.token:
            raise ControlDenied(detail='设备尚未配置 LANXI_DEVICE_TOKEN')
        url = f'{self.url}/api/v1/projects/{project}/{endpoint}'
        request = Request(url, data=None if body is None else json.dumps(body).encode(), headers={
            'Authorization': f'Bearer {self.token}', 'X-Lanxi-Device': self.device,
            'Content-Type': 'application/json',
        })
        try:
            with self.opener.open(request, timeout=2) as response:
                return json.load(response)
        except HTTPError as error:
            message = '项目控制权已变更或存在未确认的活动' if error.code == 409 else '协调服务拒绝设备请求'
            raise ControlDenied(code=error.code, detail=message) from error
        except (URLError, OSError, ValueError) as error:
            raise ControlDenied(detail='项目协调服务暂不可用') from error

    def project_at(self, cwd):
        if not cwd:
            return None
        path = Path(cwd).resolve()
        return next((self.paths[p] for p in (path, *path.parents) if p in self.paths), None)

    def event(self, project, session, source, sequence, event):
        return self.request(project, 'activity', dict(session=session, source=source, sequence=sequence, event=event))

    def restored_directory(self, project, thread):
        root = next(path for path, name in self.paths.items() if name == project)
        from handoff import limited, local_path, switch_pending
        if switch_pending(root):
            raise ControlDenied(detail='原工作区有尚未结束的切换')
        marker = root / '.git' / 'lanxi' / 'handoff.json'
        if not marker.is_file():
            return None
        try:
            record = json.loads(limited(marker, 65536))
            if record['project'] != project or record['thread'] != thread:
                return None
            mapped = root if record['cwd'] == '.' else local_path(root, record['cwd'])
            if record['version'] != 1 or not mapped.is_dir():
                raise ValueError('恢复目录无效')
            return str(mapped)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ControlDenied(detail='无法核对已恢复会话的工作目录') from error


@dataclass
class Write:
    ticket: str
    epoch: int
    revision: int = 0
    pending: int = 0
    uncertain: bool = False
    turn: str | None = None
    attached: bool = False
    needs_idle: bool = False


class ProjectWrites:
    def __init__(self, client):
        self.client = client
        self.instance = str(uuid.uuid4())
        self.tickets = {}

    def project_for(self, rpc, thread, cwd=None):
        requested = self.client.project_at(cwd)
        project = self.client.threads.get(thread)
        if thread and project is None:
            try:
                actual = rpc.call('thread/read', {'threadId': thread, 'includeTurns': False}).get('thread', {}).get('cwd')
            except (RPCError, ConnectionError, queue.Empty) as error:
                raise ControlDenied(detail='无法核对会话的项目目录') from error
            if not actual and not requested:
                raise ControlDenied(detail='无法核对会话的项目目录')
            project = self.client.project_at(actual)
        if project and requested and project != requested:
            raise ControlDenied(detail='会话与请求目录属于不同登记项目')
        return project or requested

    def reserve(self, project, thread, approval=False):
        key = project, thread
        write = self.tickets.get(key)
        group = [entry for (name, _), entry in self.tickets.items() if name == project]
        if (approval and write is None) or any(entry.uncertain for entry in group):
            raise ControlDenied(detail='会话的项目写入权已失效或仍待核对')
        existing = group[0] if group else None
        version = self.client.request(project, 'control')['version']
        ticket = self.client.request(project, 'writes', dict(
            version=version, thread=thread, instance=self.instance, ticket=existing.ticket if existing else None,
        ))['ticket']
        if write is None:
            write = self.tickets[key] = Write(ticket, existing.epoch if existing else version + 1)
        if not approval:
            write.revision += 1
            write.pending += 1
        return write

    @staticmethod
    def idle(rpc, thread, turn=None, allow_unloaded=False):
        state = rpc.call('thread/read', {'threadId': thread, 'includeTurns': False})
        status = state.get('thread', {}).get('status', {}).get('type')
        if rpc.approvals.pending_for(thread) or (status != 'idle' and not (allow_unloaded and status == 'notLoaded')):
            return False
        if not turn:
            return True
        history = rpc.call('thread/turns/list', {'threadId': thread, 'limit': 1, 'sortDirection': 'desc'})
        return any(t.get('id') == turn and t.get('status') in ('completed', 'failed', 'interrupted')
                   for t in history.get('data', []))

    def groups(self):
        groups = {}
        for (project, thread), write in self.tickets.items():
            groups.setdefault(project, {})[thread] = write
        return groups

    def release_finished(self, rpc):
        with rpc.write_lock:
            groups = self.groups()
            candidates = {project: {thread: w.revision for thread, w in group.items()}
                          for project, group in groups.items()
                          if all(not w.pending and not w.uncertain and not w.attached for w in group.values())}
        for project, revisions in candidates.items():
            group = groups[project]
            try:
                if any(w.needs_idle and not self.idle(rpc, thread, w.turn) for thread, w in group.items()):
                    continue
                with rpc.write_lock:
                    current = self.groups().get(project, {})
                    if group == current and revisions == {thread: w.revision for thread, w in current.items()}:
                        self.release_group(project, group)
            except (RPCError, ConnectionError, TimeoutError, queue.Empty):
                continue

    def yield_to_foreground(self, rpc, shutdown=False):
        if not self.tickets:
            return False
        try:
            must_yield = shutdown or any(
                self.client.request(project, 'control')['version'] != next(iter(group.values())).epoch
                for project, group in self.groups().items() if any(w.attached for w in group.values()))
            if not must_yield:
                return False
            with rpc.write_lock:
                if any(w.pending or w.uncertain for w in self.tickets.values()):
                    return False
                plan = {key: w.revision for key, w in self.tickets.items()}
                tracked = {thread: w.turn for (_, thread), w in self.tickets.items()}
                loaded = set(rpc.loaded_threads)
            if not all(self.idle(rpc, thread, tracked.get(thread), allow_unloaded=True)
                       for thread in loaded | set(tracked)):
                return False
            # Unsubscribe can leave the native writer lock behind. EOF on the
            # idle private app-server is the boundary for releasing its tickets.
            with rpc.write_lock:
                if plan != {key: w.revision for key, w in self.tickets.items()} or loaded != rpc.loaded_threads:
                    return False
                rpc.draining = True
                rpc.closed = True
                rpc.p.stdin.close()
            rpc.p.wait(timeout=3)
            self.release_closed()
            return True
        except (RPCError, OSError, queue.Empty, subprocess.TimeoutExpired):
            return False

    def release_closed(self):
        for project, group in self.groups().items():
            try:
                self.release_group(project, group)
            except ControlDenied:
                continue

    def release_group(self, project, group):
        ticket = next(iter(group.values())).ticket
        self.client.request(project, 'writes/release', {'ticket': ticket})
        for thread in group:
            del self.tickets[project, thread]


def device_client(config):
    return ControlClient(config['coordination'], os.environ.get('LANXI_DEVICE_TOKEN')) if 'coordination' in config else None


def terminal(client, cwd, args, resume=False):
    from handoff import install_resume, resume_context

    project = client.project_at(cwd) if client else None
    context = resume_context(cwd) if resume and project is None else None

    def check_context(binding, mapped_project):
        owner = client.threads.get(binding.thread)
        if (mapped_project and binding.project != mapped_project) or (owner and owner != binding.project):
            raise ControlDenied(detail='交接回执与本机登记的项目不一致')
        return mapped_project or owner

    if client and context:
        project = check_context(context, project)
        if project is None and context.project in client.paths.values():
            raise ControlDenied(detail='请把续接的线程 ID 登记到此项目，再从预备目录启动')

    def launch():
        command = args
        selected = resume_context(cwd) if resume and project and context is None else context
        if selected:
            if client:
                check_context(selected, project)
            home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
            binding = install_resume(selected, home, args[0])
            command = [*args, 'resume', '--cd', binding['cwd'], binding['thread']]
        return subprocess.Popen(command, cwd=cwd)

    if project is None:
        return launch().wait()
    session, sequence = str(uuid.uuid4()), 1
    child = None
    try:
        state = client.event(project, session, 'cli', sequence, 'open')
        if state['writer'] is not None:
            print(f'正等待后台轮次退出项目 {project}，会话 {session}。', file=sys.stderr, flush=True)
        while state['writer'] is not None:
            time.sleep(2)
            sequence += 1
            state = client.event(project, session, 'cli', sequence, 'heartbeat')
        child = launch()
        while child.poll() is None:
            try:
                return child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sequence += 1
                try:
                    client.event(project, session, 'cli', sequence, 'heartbeat')
                except ControlDenied:
                    pass  # The coordinator retains foreground occupancy while disconnected.
        return child.returncode
    finally:
        if child is None or child.poll() is not None:
            try:
                client.event(project, session, 'cli', sequence + 1, 'close')
            except ControlDenied:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('local-config.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    activity = sub.add_parser('activity')
    activity.add_argument('project')
    activity.add_argument('source', choices=('vscode', 'pycharm', 'cli'))
    activity.add_argument('session')
    activity.add_argument('sequence', type=int)
    activity.add_argument('event', choices=('open', 'heartbeat', 'close'))
    run = sub.add_parser('terminal')
    run.add_argument('--cwd', type=Path, required=True)
    run.add_argument('--resume', action='store_true')
    run.add_argument('args', nargs=argparse.REMAINDER)
    options = parser.parse_args()
    try:
        client = device_client(json.loads(options.config.read_text(encoding='utf-8-sig'))
                               if options.config.exists() else {})
        if options.command == 'terminal':
            argv = options.args[1:] if options.args[:1] == ['--'] else options.args
            if not argv:
                parser.error('需要原生 CLI 可执行文件')
            return terminal(client, options.cwd, argv, options.resume)
        if client is None:
            raise ValueError('此设备尚未配置协调服务')
        print(json.dumps(client.event(options.project, options.session, options.source,
                                      options.sequence, options.event), ensure_ascii=True))
    except (RPCError, ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'{getattr(error, "detail", str(error))}\n')


if __name__ == '__main__':
    raise SystemExit(main())
