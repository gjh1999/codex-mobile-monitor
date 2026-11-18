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

    def test_state_and_history_are_read_only(self):
        self.login()
        with patch('monitor.snapshot', return_value={'threads':[]}), patch('monitor.conversation_for', return_value={'messages':[]}) as history:
            self.assertEqual(self.client.get('/api/state?client=codex').status_code,200)
            self.assertEqual(self.client.get('/api/conversation/test?client=claude').json(),{'messages':[]})
            history.assert_called_once_with('test','claude')
        self.assertEqual(self.client.get('/api/state?client=invalid').status_code,422)

    def test_handoff_receipts_require_login_and_read_local_mapping(self):
        url = '/api/v1/handoffs'
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login()
        with patch('monitor.config', return_value={}):
            self.assertEqual(self.client.get(url).json(), {'projects': []})
        with patch('monitor.config', return_value={'handoffProjects': {'alpha': 'relative-is-invalid'}}):
            self.assertEqual(self.client.get(url).json(), {'projects': [{'project': 'alpha', 'status': 'invalid_receipt'}]})
