import concurrent.futures
import json
import queue
import tempfile
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from automation import Approvals, Scheduler
from coordination import Conflict, Ownership
from coordination_client import ControlDenied, ProjectWrites, terminal
from coordination_server import create_app
from universal_rpc import CodexRPC


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'control.sqlite3'
        self.now = 100
        self.db = Ownership(self.path, lambda: self.now)

    def test_revisions_multiroot_and_restart(self):
        ticket = self.db.reserve('vision', 'A', 'worker-a', 'thread', 0)
        old_state = self.db.status('vision')
        self.db.report('vision', 'B', 'vscode-1', 'vscode', 1, 'open')
        self.db.report('vision', 'B', 'vscode-2', 'vscode', 1, 'open')
        self.db.report('vision', 'C', 'pycharm-1', 'pycharm', 1, 'open')
        self.db.report('vision', 'C', 'pycharm-1', 'pycharm', 2, 'close')
        self.db.report('vision', 'B', 'vscode-1', 'vscode', 2, 'close')
        with self.assertRaises(Conflict):
            self.db.report('vision', 'B', 'vscode-1', 'vscode', 3, 'open')
        self.now += 60
        restarted = Ownership(self.path, lambda: self.now)
        status = restarted.status('vision')
        self.assertEqual([(h['session'], h['stale']) for h in status['humans']], [('vscode-2', True)])
        with self.assertRaises(Conflict):
            restarted.reserve('vision', 'A', 'worker-a', 'thread', old_state['version'], ticket)
        restarted.release('vision', 'A', ticket)
        with self.assertRaises(Conflict):
            restarted.reserve('vision', 'C', 'worker-c', 'thread', restarted.status('vision')['version'])
        restarted.report('vision', 'B', 'vscode-2', 'vscode', 3, 'close')
        next_ticket = restarted.reserve('vision', 'C', 'worker-c', 'thread', restarted.status('vision')['version'])
        self.assertNotEqual(next_ticket, ticket)
        with self.assertRaises(Conflict):
            restarted.release('vision', 'A', ticket)

    def test_simultaneous_background_claim_and_manual_recovery(self):
        def claim(device):
            try:
                return device, self.db.reserve('vision', device, 'instance', 'thread', 0)
            except Conflict:
                return None

        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            claims = [r for r in pool.map(claim, 'ABC') if r]
        self.assertEqual(len(claims), 1)
        device, _ = claims[0]
        state = self.db.status('vision')
        with self.assertRaises(Conflict):
            self.db.release_confirmed('vision', state['version'] - 1, device, 'thread')
        self.db.release_confirmed('vision', state['version'], device, 'thread')
        self.assertIsNone(self.db.status('vision')['writer'])

    def test_device_auth_duplicate_event_and_cli_conflict(self):
        devices = {'A': {'token': 'a' * 32, 'projects': ['vision']},
                   'B': {'token': 'b' * 32, 'projects': ['vision']}}
        with TestClient(create_app(self.path, devices)) as api:
            url = '/api/v1/projects/vision/'
            header = lambda d: {'X-Lanxi-Device': d, 'Authorization': 'Bearer ' + d.lower() * 32}
            self.assertEqual(api.get(url + 'control').status_code, 401)
            self.assertEqual(api.get('/api/v1/projects/other/control', headers=header('A')).status_code, 403)
            body = dict(session='cli-1', source='cli', sequence=1, event='open')
            opened = api.post(url + 'activity', headers=header('A'), json=body)
            self.assertEqual(opened.status_code, 200)
            self.assertEqual(api.post(url + 'activity', headers=header('A'), json=body).json(), opened.json())
            self.assertEqual(api.post(url + 'activity', headers=header('B'), json=body).status_code, 409)
            rejected = api.post(url + 'writes', headers=header('B'), json=dict(
                instance='new', thread='thread', version=opened.json()['version']))
            self.assertEqual(rejected.status_code, 409)

    def device(self, name):
        store = self.db

        class Device:
            threads = {'thread': 'vision'}

            def project_at(self, cwd):
                return 'vision' if cwd else None

            def restored_directory(self, project, thread):
                return None

            def event(self, project, session, source, sequence, event):
                return store.report(project, name, session, source, sequence, event)

            def request(self, project, path, body=None):
                try:
                    if path == 'control':
                        return store.status(project)
                    if path == 'writes':
                        return {'ticket': store.reserve(project, name, **body)}
                    return store.release(project, name, body['ticket'])
                except Conflict as error:
                    raise ControlDenied(detail=str(error)) from error

        return Device()

    def mock_rpc(self):
        rpc = CodexRPC.__new__(CodexRPC)
        rpc.project_writes = ProjectWrites(self.device('A'))
        rpc.approvals = Approvals()
        rpc.owned_threads = {'thread'}
        rpc.loaded_threads = {'thread'}
        rpc.closed = False
        rpc.draining = False
        rpc.write_lock = Lock()
        rpc.lock = Lock()
        rpc.pending = {}
        rpc.seq = 0
        rpc.sent = []
        rpc.status = 'idle'

        def write(line):
            request = json.loads(line)
            rpc.sent.append(request)
            if 'method' not in request:
                return
            method = request['method']
            if method == 'thread/read':
                result = {'thread': {'status': {'type': rpc.status}, 'updatedAt': 7}}
            elif method == 'thread/turns/list':
                result = {'data': [{'id': 'turn-1', 'status': 'completed'}]}
            elif method == 'thread/goal/get':
                result = {'goal': {'status': 'active'}}
            else:
                result = {'turn': {'id': 'turn-1'}}
            rpc.dispatch({'id': request['id'], 'result': result})

        rpc.p = SimpleNamespace(stdin=SimpleNamespace(write=write, flush=lambda: None, close=Mock()), wait=Mock())
        return rpc

    def test_guarded_transport_blocks_approval_until_idle_drain(self):
        rpc = self.mock_rpc()
        rpc.call('turn/start', {'threadId': 'thread'})
        token = rpc.approvals.add(rpc, {'id': 'approval', 'method': 'item/commandExecution/requestApproval',
                                       'params': {'threadId': 'thread', 'command': 'echo fixture'}})
        self.db.report('vision', 'B', 'ide-1', 'vscode', 1, 'open')
        self.assertEqual(rpc.approvals.decide(token, True), 'control_lost')
        self.assertNotIn('approval', [request['id'] for request in rpc.sent])
        with self.assertRaises(ControlDenied):
            rpc.call('turn/steer', {'threadId': 'thread', 'expectedTurnId': 'turn-1'})
        rpc.status = 'active'
        self.assertFalse(rpc.reconcile_writes())
        rpc.status = 'idle'
        self.assertFalse(rpc.reconcile_writes(), 'an unresolved tool approval prevents drain')
        self.assertEqual(rpc.approvals.decide(token, False), 'denied')
        self.assertTrue(rpc.reconcile_writes())
        rpc.p.stdin.close.assert_called_once()
        self.assertIsNone(self.db.status('vision')['writer'])

    def test_scheduler_retries_same_revision_after_known_denial(self):
        rpc = self.mock_rpc()
        task = {'threadId': 'thread', 'enabled': True, 'exclusiveOwner': True, 'prompt': 'fixture'}
        scheduler = Scheduler()
        self.addCleanup(scheduler.db.close)
        self.db.report('vision', 'C', 'ide-1', 'pycharm', 1, 'open')
        self.assertEqual(scheduler.tick(rpc, [task], 10), ['blocked:project_control'])
        self.db.report('vision', 'C', 'ide-1', 'pycharm', 2, 'close')
        self.assertEqual(scheduler.tick(rpc, [task], 16), ['sent:thread'])
        self.assertEqual(sum(r.get('method') == 'turn/start' for r in rpc.sent), 1)

    def test_timeout_keeps_control_until_verified_and_threads_share_project(self):
        rpc = self.mock_rpc()
        rpc.call('thread/resume', {'threadId': 'thread'})
        rpc.call('thread/resume', {'threadId': 'thread-two', 'cwd': str(self.path.parent)})
        tickets = {w.ticket for w in rpc.project_writes.tickets.values()}
        self.assertEqual(len(tickets), 1)
        original = rpc.p.stdin.write

        def silence_turn(data):
            if json.loads(data).get('method') != 'turn/start':
                original(data)

        rpc.p.stdin.write = silence_turn
        with self.assertRaises(queue.Empty):
            rpc.call('turn/start', {'threadId': 'thread'}, timeout=0.01)
        self.db.report('vision', 'B', 'ide', 'vscode', 1, 'open')
        self.assertFalse(rpc.reconcile_writes())
        self.assertEqual(self.db.status('vision')['writer']['device'], 'A')
        rpc.p.stdin.close.assert_not_called()

    def test_terminal_holds_foreground_while_child_runs(self):
        client = self.device('B')
        token = self.db.reserve('vision', 'A', 'worker-a', 'thread', 0)

        def drain_and_check_wait(_):
            state = self.db.status('vision')
            self.assertEqual(state['humans'][0]['source'], 'cli')
            self.db.release('vision', 'A', token)

        def start(_, cwd):
            state = self.db.status('vision')
            self.assertIsNone(state['writer'])
            self.assertEqual(len(state['humans']), 1)
            with self.assertRaises(Conflict):
                self.db.reserve('vision', 'C', 'worker-c', 'thread', state['version'])
            return SimpleNamespace(poll=lambda: 0, returncode=0)

        with patch('coordination_client.time.sleep', side_effect=drain_and_check_wait), \
             patch('coordination_client.subprocess.Popen', side_effect=start):
            self.assertEqual(terminal(client, self.path.parent, ['codex']), 0)
        self.assertEqual(self.db.status('vision')['humans'], [])


if __name__ == '__main__':
    unittest.main()
