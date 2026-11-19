"""Universal RPC interface supporting both Codex and Claude Code."""
import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from automation import APPROVAL_METHODS


from rpc_errors import RPCError
from coordination_client import ControlDenied, ProjectWrites


class UniversalRPC:
    """Base RPC client for Anthropic CLI tools."""

    def __init__(self, executable_name: str, journal=None, custom_process=None, env_var: Optional[str] = None):
        """
        Args:
            executable_name: 'codex' or 'claude'
            journal: Optional journal for recording operations
            custom_process: Override process detection
            env_var: Optional environment variable to check for executable path
        """
        self.journal = journal
        self.pending = {}
        self.seq = 0
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.owned_threads = set()
        self.closed = False
        self.executable_name = executable_name

        if custom_process is None:
            process = self._find_process(executable_name, env_var)
        else:
            process = custom_process

        self.p = process
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.call('initialize', {
                'clientInfo': {
                    'name': 'mobile_monitor',
                    'version': '0.4.0',
                    'client': executable_name
                },
                'capabilities': {'experimentalApi': True}
            })
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    @staticmethod
    def _find_process(executable_name: str, env_var: Optional[str] = None):
        """Locate and launch the executable."""
        exe = None

        # Check environment variable first
        if env_var and env_var in os.environ:
            exe = os.environ[env_var]

        # Try PATH
        if not exe:
            exe = shutil.which(f'{executable_name}.exe')

        # Try npm global modules
        if not exe and executable_name == 'claude':
            try:
                exe = str(next(
                    (Path(os.environ['APPDATA']) / 'npm/node_modules/@anthropic-ai/claude-code')
                    .rglob('claude.exe')
                ))
            except (StopIteration, KeyError):
                pass

        if not exe and executable_name == 'codex':
            npm = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules/@openai/codex'
            exe = next((str(p) for p in npm.glob('**/codex.exe')), None)
        if not exe:
            exe = shutil.which(executable_name)
        if not exe:
            raise FileNotFoundError(f"Cannot find {executable_name} CLI; install it or add it to PATH")


        process = subprocess.Popen(
            [exe, 'app-server', '--stdio'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            bufsize=1,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        return process

    def send(self, x: Dict[str, Any], guard=None):
        """Recheck local stop intent after acquiring the transport write lock."""
        with self.write_lock:
            if guard is not None and not guard():
                raise RPCError(detail='任务状态已变化，未发送指令。')
            if self.closed:
                raise ConnectionError('RPC disconnected')
            self.p.stdin.write(json.dumps(x) + '\n')
            self.p.stdin.flush()

    def dispatch(self, x: Dict[str, Any]):
        """Dispatch incoming message."""
        if getattr(self, 'journal', None):
            self.journal.record(x, self.owned_threads)
        if 'id' in x and 'method' in x:
            if x['method'] in APPROVAL_METHODS:
                # Handle approval if available
                if hasattr(self, 'approvals'):
                    self.approvals.add(self, x)
            else:
                self.send({'id': x['id'], 'error': {'code': -32601, 'message': 'Unsupported request'}})
        elif 'id' in x:
            with self.lock:
                q = self.pending.get(x['id'])
            if q:
                q.put(x)
        elif x.get('method') == 'serverRequest/resolved':
            if hasattr(self, 'approvals'):
                self.approvals.resolve(self, x.get('params', {}).get('requestId'))

    def read(self):
        """Read from process stdout."""
        try:
            for line in self.p.stdout:
                try:
                    x = json.loads(line)
                except ValueError:
                    continue
                self.dispatch(x)
        finally:
            self.mark_closed()

    def mark_closed(self):
        """Mark RPC as closed."""
        self.closed = True
        if hasattr(self, 'approvals'):
            self.approvals.disconnect(self)
        with self.lock:
            for q in self.pending.values():
                q.put({'disconnected': True})

    def close(self):
        """Close RPC connection."""
        self.mark_closed()
        if self.p.poll() is None:
            self.p.terminate()

    def call(self, method: str, params: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
        """Make RPC call; local guards never cross the JSON-RPC boundary."""
        params = dict(params)
        guard = params.pop('_sendGuard', None)
        with self.lock:
            self.seq += 1
            i = self.seq
            q = queue.Queue()
            self.pending[i] = q
        try:
            self.send({'id': i, 'method': method, 'params': params}, guard=guard)
            x = q.get(timeout=timeout)
            if x.get('disconnected'):
                raise ConnectionError('RPC disconnected')
            if 'error' in x:
                error = x['error'] if isinstance(x['error'], dict) else {}
                raise RPCError(
                    'RPC request rejected',
                    code=error.get('code'),
                    detail=str(error.get('message', ''))[:500]
                )
            result = x.get('result', {})
            if method == 'turn/start' and getattr(self, 'journal', None):
                self.journal.track_turn(params['threadId'], (result.get('turn') or {}).get('id'))
            return result
        finally:
            with self.lock:
                self.pending.pop(i, None)


class CodexRPC(UniversalRPC):
    """Codex-specific RPC client."""
    project_writes = None

    def __init__(self, approvals, journal=None, custom_process=None, coordination=None):
        self.approvals = approvals
        self.project_writes = ProjectWrites(coordination) if coordination and coordination.paths else None
        self.loaded_threads = set()
        self.draining = False
        super().__init__('codex', journal=journal, custom_process=custom_process)

    def call(self, method, params, timeout=20):
        automatic_resume = method == 'thread/resume' and not params.get('cwd')
        if automatic_resume and getattr(self, 'personal_session', None):
            session = self.personal_session(params['threadId'])
            if session:
                params = {**params, 'cwd': session['cwd'], 'path': session['path']}
        writes = self.project_writes
        if not writes:
            return super().call(method, params, timeout)
        if method == 'thread/start':
            result = super().call(method, params, timeout)
            self.loaded_threads.add(result['thread']['id'])
            return result
        if method not in ('turn/start', 'turn/steer', 'thread/goal/set', 'thread/resume', 'thread/settings/update'):
            return super().call(method, params, timeout)
        thread = params['threadId']
        project = writes.project_for(self, thread, params.get('cwd'))
        if project is None:
            result = super().call(method, params, timeout)
            if method == 'thread/resume':
                self.loaded_threads.add(thread)
            return result
        previous_guard = params.get('_sendGuard')
        reservation = None

        def guard():
            nonlocal reservation
            if previous_guard and not previous_guard():
                return False
            try:
                reservation = writes.reserve(project, thread)
            except ControlDenied as error:
                if method == 'thread/resume':
                    raise ControlDenied(detail='active writer: ' + error.detail) from error
                raise
            return previous_guard is None or previous_guard()

        confirmed = False
        accepted = False
        turn = None
        wired = {**params, '_sendGuard': guard}
        if automatic_resume:
            mapped = writes.client.restored_directory(project, thread)
            if params.get('cwd') and writes.client.project_at(params['cwd']) != project:
                raise ControlDenied(detail='个人会话目录与协调服务登记的项目不一致')
            if mapped and not params.get('cwd'):
                wired['cwd'] = mapped
        try:
            result = super().call(method, wired, timeout)
            accepted = True
            if method == 'thread/resume':
                self.loaded_threads.add(thread)
            elif method == 'turn/start':
                turn = result.get('turn', {}).get('id')
            elif method == 'turn/steer':
                turn = params.get('expectedTurnId')
            confirmed = method not in ('turn/start', 'turn/steer') or bool(turn)
            return result
        except RPCError:
            # An explicit RPC error settles a sent request. A refused preflight
            # never created a new local reservation for this request.
            confirmed = True
            raise
        finally:
            if reservation is not None:
                with self.write_lock:
                    reservation.pending -= 1
                    reservation.uncertain = reservation.uncertain or not confirmed
                    if thread in self.loaded_threads:
                        reservation.attached = True
                    reservation.needs_idle |= accepted
                    if turn:
                        reservation.turn = turn

    def approval_guard(self, thread):
        writes = self.project_writes
        if not writes:
            return None
        if not thread:
            raise ControlDenied(detail='无法核对此审批所属的线程')
        project = writes.project_for(self, thread)
        if project is None:
            return None

        def guard():
            writes.reserve(project, thread, approval=True)
            return True

        return guard

    def reconcile_writes(self, shutdown=False):
        self.project_writes.release_finished(self)
        return self.project_writes.yield_to_foreground(self, shutdown)


# Claude Code uses its own stream-json control protocol, not Codex app-server.
from claude_rpc import ClaudeCodeRPC

