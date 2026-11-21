import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from automation import Approvals
from codex_watch import RolloutWatch, SharedCodex
from codex_workspace import CodexWorkspace
from personal_relay import Records


THREAD = '11111111-2222-4333-8444-555555555555'
METHOD = 'item/commandExecution/requestApproval'


def record(kind, payload, when=None):
    return json.dumps({'type': kind, 'payload': payload,
                       'timestamp': datetime.fromtimestamp(when or time.time(), timezone.utc).isoformat()}).encode() + b'\n'


class CodexWatchTests(unittest.TestCase):
    def test_unattached_rollout_completion_and_split_append(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / 'sessions' / '2026' / '11' / '21'
            sessions.mkdir(parents=True)
            path = sessions / f'rollout-fixture-{THREAD}.jsonl'
            now = time.time()
            path.write_bytes(record('session_meta', {'id': THREAD, 'cwd': str(home / 'Project A')})
                             + record('event_msg', {'type': 'task_complete', 'turn_id': 'old'}, now - 60))
            watcher = RolloutWatch(home, now - 1)
            stop = threading.Event()
            self.assertEqual(list(watcher.scan(stop)), [])
            completion = record('event_msg', {'type': 'task_complete', 'turn_id': 'new'}, now)
            with path.open('ab') as stream:
                stream.write(record('event_msg', {'type': 'turn_aborted', 'turn_id': 'aborted'}) + completion[:20])
            self.assertEqual(list(watcher.scan(stop)), [])
            with path.open('ab') as stream:
                stream.write(completion[20:])
            self.assertEqual(list(watcher.scan(stop)), [{'threadId': THREAD, 'turnId': 'new', 'time': now, 'title': 'Project A'}])
            self.assertEqual(list(watcher.scan(stop)), [])

    def test_shared_daemon_observation_uses_original_request(self):
        peer = SharedCodex.__new__(SharedCodex)
        peer.approvals = Approvals()
        peer.subscriptions = set()
        peer.send = Mock()
        ordinary = '22222222-3333-4444-8555-666666666666'
        statuses = {THREAD: {'type': 'active', 'activeFlags': ['waitingOnApproval']}, ordinary: {'type': 'idle'}}

        def call(method, params, timeout):
            if method == 'thread/loaded/list':
                return {'data': list(statuses)}
            thread = params['threadId']
            if method == 'thread/resume':
                peer.dispatch({'id': 12, 'method': METHOD, 'params': {'threadId': THREAD, 'command': 'echo example'}})
            return {'thread': {'id': thread, 'status': statuses[thread]}}

        peer.call = Mock(side_effect=call)
        peer.poll_approvals(threading.Event())
        resumed = [args[0][1] for args in peer.call.call_args_list if args[0][0] == 'thread/resume']
        self.assertEqual(resumed, [{'threadId': THREAD, 'excludeTurns': True}])
        entry, = peer.approvals.pending('codex')
        self.assertFalse(peer.send.called)
        self.assertEqual(peer.approvals.decide_pending(entry['token'], True), 'allowed')
        peer.send.assert_called_once_with({'id': 12, 'result': {'decision': 'accept'}})

    def test_workspace_completion_and_selected_approval_round_trip(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'relay.sqlite3'
            def store(device, space='same'):
                relay = Records(path, space)
                relay.client = SimpleNamespace(device=device)
                return relay

            approvals = Approvals(clock=lambda: now)
            peer = SimpleNamespace(send=Mock(), client_type='codex')
            chosen = approvals.add(peer, {'id': 1, 'method': METHOD, 'params': {'command': 'echo one'}})
            remaining = approvals.add(peer, {'id': 2, 'method': METHOD, 'params': {'command': 'echo two'}})
            source = CodexWorkspace(approvals, lambda: now)
            viewer = CodexWorkspace(Approvals(clock=lambda: now), lambda: now)
            outsider = CodexWorkspace(Approvals(clock=lambda: now), lambda: now)
            source.complete({'threadId': THREAD, 'turnId': 'finished', 'time': now, 'title': 'Fixture'})
            source.sync(store('desktop'))
            turns = viewer.sync(store('laptop'))
            self.assertEqual([turn['turnId'] for turn in turns], ['finished'])
            self.assertEqual({item['sourceToken'] for item in viewer.pending()}, {chosen, remaining})
            self.assertEqual(outsider.sync(store('third', 'other')), [])
            self.assertEqual(outsider.pending(), [])
            token = f'remote:desktop:{chosen}'
            self.assertEqual(viewer.decide(token, True), 'queued')
            self.assertEqual(viewer.decide(token, True), 'queued')
            self.assertFalse(peer.send.called)
            viewer.sync(store('laptop'))
            source.sync(store('desktop'))
            viewer.sync(store('laptop'))
            self.assertEqual(viewer.decisions(), {token: 'allowed'})
            self.assertEqual([item['sourceToken'] for item in viewer.pending()], [remaining])
            source.sync(store('desktop'))
            peer.send.assert_called_once_with({'id': 1, 'result': {'decision': 'accept'}})
            now += 40
            self.assertFalse(viewer.pending())


if __name__ == '__main__':
    unittest.main()
