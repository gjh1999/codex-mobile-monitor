import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from model_groups import Organizer, read_settings, update_settings


class ModelGroupsTest(unittest.TestCase):
    def test_background_classifies_all_surfaces_and_preserves_manual_move(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            received = []
            entered, release = threading.Event(), threading.Event()

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    item = json.loads(request['messages'][-1]['content'])
                    received.append((self.path, request['model'], item))
                    if item['category'] == 'todo':
                        entered.set()
                        release.wait(3)
                    names = [None if row['title'] == '未知' else '澜汐开发' for row in item['items']]
                    content = json.dumps({'choices': [{'message': {'content': json.dumps({'groups': names})}}]})
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(content.encode())

                def log_message(self, *_args):
                    pass

            with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                update_settings(root, f'http://127.0.0.1:{server.server_port}/v1', 'mock-model')
                changes = []
                organizer = Organizer(root, changes.append)
                try:
                    organizer.observe('task:codex', 'codex-id', '审查提交', project='澜汐')
                    organizer.observe('task:claude', 'claude-id', '完善体验')
                    organizer.observe('note', 1, '', '记下海浪动效的想法')
                    organizer.observe('note', 2, '未知')
                    organizer.observe('todo', 1, '', '准备安装文件')
                    self.assertTrue(entered.wait(5))
                    organizer.assign('todo', 1, '本周发布')
                    release.set()
                    deadline = time.monotonic() + 5
                    while (organizer.busy or organizer.pending) and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertFalse(organizer.busy or organizer.pending)
                    self.assertEqual(organizer.label('task:codex', 'codex-id'), '澜汐开发')
                    self.assertEqual(organizer.label('task:claude', 'claude-id'), '澜汐开发')
                    self.assertEqual(organizer.label('note', 1), '澜汐开发')
                    self.assertEqual(organizer.label('note', 2), '')
                    self.assertEqual(organizer.label('todo', 1), '本周发布')
                    self.assertEqual({row[0] for row in received}, {'/v1/chat/completions'})
                    self.assertEqual({row[1] for row in received}, {'mock-model'})
                    self.assertEqual(Organizer(root, lambda _: None).label('todo', 1), '本周发布')
                finally:
                    release.set()
                    organizer.close()
                    if organizer.worker:
                        organizer.worker.join(4)
                    server.shutdown()
                    thread.join(3)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI only')
    def test_api_key_encrypted_reopened_and_not_reused_at_other_url(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secret = 'lanxi-test-key-keep-private'
            endpoint = 'https://model.example.com/v1'
            try:
                update_settings(root, endpoint, 'model', secret)
            except OSError as error:
                if getattr(error, 'winerror', None) == 2:
                    self.skipTest('Windows user profile is unavailable to DPAPI')
                raise
            self.assertNotIn(secret, (root / 'local-model-api.json').read_text())
            self.assertEqual(read_settings(root).key(), secret)
            update_settings(root, endpoint, 'other-model')
            self.assertEqual(read_settings(root).key(), secret)
            with self.assertRaisesRegex(ValueError, '接口地址已更换'):
                update_settings(root, 'https://other.example.com/v1', 'other-model')
            self.assertEqual(read_settings(root).base_url, endpoint)
            update_settings(root, 'https://other.example.com/v1', 'other-model', '')
            self.assertEqual(read_settings(root).key(), '')
