"""End-to-end local scheduling tests: real controller + Claude protocol peer."""
import tempfile
import time
import unittest
from pathlib import Path

from task_control import TaskControl
from tests import test_claude as fixtures

TID = fixtures.TID

# Reuse only the protocol/session fixture, not its inherited test methods.
class AutoProgressTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown
    append = fixtures.Tests.append
    history = fixtures.Tests.history
    until = fixtures.Tests.until
    attach = fixtures.Tests.attach

    def enable(self):
        session = self.attach()
        self.control.submit(TID, 'manage', request_id='enable-auto')
        self.control.tick(self.rpc)
        self.assertTrue(self.control.snapshot()['tasks'][0]['managed'])
        self.assertFalse([e for e in session.process.sent if e['type'] == 'user'])
        return session

    def due(self):
        return self.control.db.execute('SELECT due FROM auto_progress WHERE thread=?', (TID,)).fetchone()[0]

    def test_full_path_enable_due_send_once_and_restart_preserves_timer(self):
        session = self.enable()
        due = self.due()
        self.assertEqual(self.control.tick_auto_progress(self.rpc, due - 1), [])
        self.assertEqual(self.control.tick_auto_progress(self.rpc, due), ['sent'])
        users = [e for e in session.process.sent if e['type'] == 'user']
        self.assertEqual(len(users), 1)
        self.assertTrue(users[0]['uuid'])
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'succeeded')
        self.assertEqual(self.control.tick_auto_progress(self.rpc, due), [])
        session.dispatch({'type': 'result', 'is_error': False, 'user_message_uuid': users[0]['uuid']})
        next_due = self.due()
        self.control.db.close()
        self.control = TaskControl(self.root / 'control.db')
        self.control.tick(self.rpc)
        self.assertTrue(self.control.snapshot()['tasks'][0]['managed'])
        self.assertEqual(self.due(), next_due)
        self.assertEqual(self.control.tick_auto_progress(self.rpc, next_due - 1), [])
        self.control.submit(TID, 'attach', request_id='reattach-preserves-auto')
        self.control.tick(self.rpc)
        self.assertTrue(self.control.snapshot()['tasks'][0]['managed'])
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)

    def test_no_implicit_enable_from_attach_or_claude_seed(self):
        session = self.attach()
        self.assertFalse(self.control.snapshot()['tasks'][0]['managed'])
        self.assertEqual(self.control.tick_auto_progress(self.rpc, time.time() + 100000), [])
        self.assertFalse([e for e in session.process.sent if e['type'] == 'user'])
        with tempfile.TemporaryDirectory() as folder:
            control = TaskControl(Path(folder) / 'seed.db')
            try:
                control.seed([TID], managed=False)
                self.assertFalse(control.snapshot()['tasks'][0]['managed'])
            finally:
                control.db.close()

    def test_pause_unmanage_and_detach_block_due_send(self):
        session = self.enable()
        for index, action in enumerate(('pause', 'unmanage', 'detach')):
            if index:
                self.control.submit(TID, 'attach', request_id='attach-' + action)
                self.control.tick(self.rpc)
                self.control._execute('UPDATE targets SET paused=0 WHERE id=?', (TID,))
                self.control.submit(TID, 'manage', request_id='enable-' + action)
                self.control.tick(self.rpc)
            due = self.due()
            self.control.submit(TID, action, request_id='block-' + action)
            if action != 'pause':
                self.control.tick(self.rpc)
            self.assertEqual(self.control.tick_auto_progress(self.rpc, due), [])
            self.assertFalse([e for e in session.process.sent if e['type'] == 'user'])
            self.control.tick(self.rpc)

    def test_busy_waits_and_does_not_insert_automatic_priority_message(self):
        session = self.enable()
        self.control.submit(TID, 'message', 'manual work', 'manual-before-auto')
        self.control.tick(self.rpc)
        due = self.due()
        self.assertEqual(self.control.tick_auto_progress(self.rpc, due), ['blocked:conditions'])
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)

    def test_approval_error_and_external_writer_each_block_send(self):
        session = self.enable()
        due = self.due()
        self.approvals.clock = lambda: due
        session.dispatch({'type': 'control_request', 'request_id': 'approve', 'request': {
            'subtype': 'can_use_tool', 'tool_name': 'Bash', 'input': {'command': 'git status'}}})
        self.assertEqual(self.control.tick_auto_progress(self.rpc, due), ['blocked:conditions'])
        session.dispatch({'type': 'control_cancel_request', 'request_id': 'approve'})
        session.last_error = 'Rate limited'
        self.assertEqual(self.control.tick_auto_progress(self.rpc, self.due()), ['blocked:conditions'])
        session.last_error = None
        original = self.rpc.control_available
        self.rpc.control_available = lambda tid: False
        try:
            self.assertEqual(self.control.tick_auto_progress(self.rpc, self.due()), ['blocked:conditions'])
        finally:
            self.rpc.control_available = original
        self.assertFalse([e for e in session.process.sent if e['type'] == 'user'])

    def test_uncertain_delivery_disables_auto_and_is_not_retried_on_restart(self):
        session = self.enable()
        original = session.process.write
        def uncertain(line):
            original(line)
            if '"type": "user"' in line:
                raise OSError('delivery uncertain')
        session.process.write = uncertain
        self.assertEqual(self.control.tick_auto_progress(self.rpc, self.due()), ['blocked:delivery'])
        self.assertFalse(self.control.snapshot()['tasks'][0]['managed'])
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'unknown')
        self.control.db.close()
        self.control = TaskControl(self.root / 'control.db')
        self.control.tick(self.rpc)
        self.assertEqual(self.control.tick_auto_progress(self.rpc, time.time() + 100000), [])
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)

    def test_crash_reservation_blocks_automatic_replay(self):
        self.enable()
        self.control._execute("UPDATE auto_progress SET state='reserved' WHERE thread=?", (TID,))
        self.control.db.close()
        self.control = TaskControl(self.root / 'control.db')
        self.assertFalse(self.control.snapshot()['tasks'][0]['managed'])
        self.assertEqual(self.control.snapshot()['tasks'][0]['autoProgress']['state'], 'unknown')
        self.control.tick(self.rpc)
        self.assertEqual(self.control.tick_auto_progress(self.rpc, time.time() + 100000), [])

    def test_pause_between_read_and_write_is_rechecked_by_send_guard(self):
        session = self.enable()
        original = self.rpc.call
        def cross_pause(method, params, **options):
            if method == 'turn/start':
                self.control.submit(TID, 'pause', request_id='stop-before-auto-write')
            return original(method, params, **options)
        self.rpc.call = cross_pause
        try:
            self.assertEqual(self.control.tick_auto_progress(self.rpc, self.due()), ['blocked:changed'])
        finally:
            self.rpc.call = original
        self.assertTrue(self.control.snapshot()['tasks'][0]['paused'])
        self.assertFalse([e for e in session.process.sent if e['type'] == 'user'])
        self.control.tick(self.rpc)


if __name__ == '__main__':
    unittest.main()
