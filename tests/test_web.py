import os
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from web_server import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, MONITOR_PASSWORD='test-password-123456'):
            self.app = create_app(start_workers=False)
        self.client = TestClient(self.app)

    def login(self):
        self.assertEqual(self.client.post('/api/login', json={'password':'test-password-123456'}).status_code, 200)

    def test_auth_origin_and_csrf(self):
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'password':'wrong'}).status_code, 401)
        self.login()
        self.assertEqual(self.client.post('/api/logout', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/logout', json={}, headers={'x-monitor-request':'1','origin':'https://attacker.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/logout', json={}, headers={'x-monitor-request':'1'}).status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 401)

    def test_operation_uses_existing_controller(self):
        self.login()
        controller = Mock()
        controller.submit.return_value = 'op-test'
        data = {'client':'codex','thread':'00000000-0000-4000-8000-000000000001','action':'message','text':'继续','request_id':'request-123456789'}
        with patch('monitor.get_controller', return_value=controller):
            response = self.client.post('/api/operations', json=data, headers={'x-monitor-request':'1'})
        self.assertEqual(response.json(), {'id':'op-test'})
        controller.submit.assert_called_once_with(data['thread'], 'message', '继续', data['request_id'])
        data['action']='arbitrary-shell'
        self.assertEqual(self.client.post('/api/operations',json=data,headers={'x-monitor-request':'1'}).status_code,422)

    def test_approval_unknown_is_not_success(self):
        self.login()
        with patch('monitor.approvals.decide', return_value='unknown_delivery'):
            r=self.client.post('/api/approvals',json={'token':'test','allow':True},headers={'x-monitor-request':'1'})
        self.assertEqual(r.status_code,409)

    def test_approval_batch_requires_explicit_tokens_and_reports_each_decision(self):
        import monitor
        from automation import Approvals

        self.assertEqual(self.client.get('/api/approvals').status_code, 401)
        self.login()
        approvals = Approvals()
        rpc = Mock(send=Mock(), approval_guard=None, client_type='codex')
        method = 'item/commandExecution/requestApproval'
        first = approvals.add(rpc, {'id': 1, 'method': method, 'params': {'command': 'echo one'}})
        second = approvals.add(rpc, {'id': 2, 'method': method, 'params': {'command': 'echo two'}})
        with patch.object(monitor, 'approvals', approvals):
            pending = self.client.get('/api/approvals').json()['pending']
            self.assertEqual([item['token'] for item in pending], [first, second])
            payload = {'tokens': [first, 'missing', first], 'allow': True}
            self.assertEqual(self.client.post('/api/approvals/batch', json=payload).status_code, 403)
            headers = {'x-monitor-request': '1'}
            result = self.client.post('/api/approvals/batch', json=payload, headers=headers)
            self.assertEqual(result.json(), {'results': [{'token': first, 'decision': 'allowed'},
                                                         {'token': 'missing', 'decision': 'not_found'}]})
            self.assertEqual(self.client.post('/api/approvals/batch', json={'tokens': [second], 'allow': 'true'},
                                              headers=headers).status_code, 422)
            self.assertEqual([item['token'] for item in approvals.pending()], [second])
        rpc.send.assert_called_once_with({'id': 1, 'result': {'decision': 'accept'}})

    def test_state_and_history_are_read_only(self):
        self.login()
        with patch('monitor.snapshot', return_value={'threads':[]}), patch('monitor.conversation_for', return_value={'messages':[]}) as history:
            self.assertEqual(self.client.get('/api/state?client=codex').status_code,200)
            for client in ('codex', 'claude'):
                self.assertEqual(self.client.get(f'/api/conversation/test?client={client}').json(),{'messages':[]})
                history.assert_called_with('test',client,local_history=False)
        self.assertEqual(self.client.get('/api/state?client=invalid').status_code,422)

    def test_handoff_receipts_require_login_and_read_local_mapping(self):
        url = '/api/v1/handoffs'
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login()
        with patch('monitor.config', return_value={}):
            self.assertEqual(self.client.get(url).json(), {'projects': []})
        with patch('monitor.config', return_value={'handoffProjects': {'alpha': 'relative-is-invalid'}}):
            self.assertEqual(self.client.get(url).json(), {'projects': [{'project': 'alpha', 'status': 'invalid_receipt'}]})

    def test_handoff_sync_requires_login_and_reads_worker_state(self):
        url = '/api/v1/handoff-sync'
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login()
        with patch('monitor.config', return_value={}):
            self.assertEqual(self.client.get(url).json(), {'status': 'disabled', 'projects': {}})
        state = {'projects': {'vision': {'received': [{'device': 'office', 'status': 'awaiting_package'}]}}}
        with patch('monitor.config', return_value={'handoffSync': {'device': 'laptop'}}), \
                patch('monitor.snapshot', return_value={'handoffSyncStatus': 'running'}), \
                patch('web_server.read_handoff_sync', return_value=state):
            self.assertEqual(self.client.get(url).json(), {**state, 'status': 'running'})

    def test_project_control_requires_login_and_reports_coordinator_outage(self):
        from coordination_client import ControlDenied
        url = '/api/v1/project-control'
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login()
        client = Mock(paths={str(os.getcwd()): 'vision'})
        client.request.side_effect = ControlDenied(detail='coordinator offline')
        with patch('web_server.device_client', return_value=client):
            self.assertEqual(self.client.get(url).json(), {'projects': [{'project': 'vision', 'status': 'unavailable'}]})

    def test_embedded_web_reads_the_desktop_worker_state(self):
        import socket
        import tempfile
        import time
        from pathlib import Path
        from urllib.error import URLError
        from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request
        import monitor

        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        states = {'shared_worker': True, 'threads': []}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(monitor, 'ROOT', Path(directory)), \
                patch.object(monitor, 'poll', lambda stop: stop.wait()), \
                patch.object(monitor, 'poll_claude', lambda stop: stop.wait()), \
                patch.object(monitor, 'snapshot', return_value=states), \
                patch.dict(os.environ, MONITOR_PASSWORD='test-password-123456', MONITOR_HOST='127.0.0.1',
                           MONITOR_PORT=str(port), MONITOR_HTTPS='0', DINGTALK_CLIENT_ID=''):
            stop, workers = monitor.start_background_service(embed_web=True)
            opener = build_opener(ProxyHandler({}), HTTPCookieProcessor())
            url = f'http://127.0.0.1:{port}'
            try:
                deadline = time.monotonic() + 5
                login = Request(url + '/api/login', data=b'{"password":"test-password-123456"}',
                                headers={'Content-Type': 'application/json'})
                while True:
                    try:
                        with opener.open(login, timeout=1) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except URLError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                with opener.open(url + '/api/state', timeout=1) as response:
                    self.assertIn(b'"shared_worker":true', response.read())
                self.assertEqual(
                    {worker.name for worker in workers.threads},
                    {'monitor-worker', 'claude-monitor-worker', 'codex-watch', 'project-library', 'monitor-web'},
                )
            finally:
                stop.set()
                workers.join(6)
            self.assertFalse(workers.is_alive())
