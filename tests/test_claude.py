import io
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from automation import Approvals
from claude_rpc import ClaudeCodeRPC
from claude_sessions import ClaudeSessions
from reply_log import ReplyLog
from rpc_errors import RPCError
from task_control import TaskControl

TID = '11111111-2222-4333-8444-555555555555'


class Lines:
    def __init__(self):
        self.lines = queue.Queue()
        self.reader = None
    def __iter__(self):
        self.reader = threading.current_thread()
        return self
    def __next__(self):
        line = self.lines.get()
        if line is None:
            raise StopIteration
        return line


class FakeProcess:
    """A subprocess protocol peer, not a model mock returning canned app results."""
    def __init__(self, command, **options):
        self.command, self.options = command, options
        self.pid = 12345
        self.code = None
        self.stdout, self.stderr = Lines(), Lines()
        self.stdin = self
        self.sent = []
        self.delay_interrupt_result = False
    def write(self, line):
        event = json.loads(line)
        self.sent.append(event)
        if event.get('type') == 'control_request':
            self.emit({'type': 'control_response', 'response': {
                'subtype': 'success', 'request_id': event['request_id'], 'response': {}}})
            if event['request']['subtype'] == 'interrupt' and not self.delay_interrupt_result:
                self.emit({'type': 'result', 'is_error': False, 'result': ''})
    def flush(self):
        pass
    def emit(self, event):
        self.stdout.lines.put(json.dumps(event) + '\n')
    def poll(self):
        return self.code
    def terminate(self):
        self.code = 0
        self.stdout.lines.put(None)
        self.stderr.lines.put(None)
    def kill(self):
        self.terminate()
    def wait(self, timeout=None):
        return self.code
    def wait_readers(self, timeout=2):
        for stream in (self.stdout, self.stderr):
            if stream.reader and stream.reader is not threading.current_thread():
                stream.reader.join(timeout=timeout)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / 'projects/project' / (TID + '.jsonl')
        self.log.parent.mkdir(parents=True)
        self.sessions = ClaudeSessions(self.root, alive=lambda pid: pid == 777)
        self.approvals = Approvals()
        self.journal = ReplyLog(self.root / 'replies.db')
        self.rpc = ClaudeCodeRPC(self.approvals, self.journal, self.sessions, ['claude-test'], FakeProcess)
        self.control = TaskControl(self.root / 'control.db')
    def tearDown(self):
        self.rpc.close()
        for session in list(self.rpc.running.values()):
            session.process.wait_readers()
        self.control.db.close()
        self.journal.db.close()
        self.tmp.cleanup()
    def append(self, event):
        with self.log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'sessionId': TID, 'cwd': str(self.root), **event}) + '\n')
    def history(self):
        self.append({'type': 'user', 'message': {'content': 'existing task'}})
        self.append({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'done'}], 'stop_reason': 'end_turn'}})
    def until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            threading.Event().wait(.005)
        self.assertTrue(predicate())
    def attach(self):
        self.history()
        self.control.submit(TID, 'attach')
        self.control.tick(self.rpc)
        self.assertTrue(self.control.snapshot()['tasks'][0]['canControl'])
        return self.rpc.running[TID]
    def test_incremental_transcripts_partial_lines_and_title(self):
        self.history()
        first = self.sessions.threads()[0]
        self.assertEqual(first['title'], 'existing task')
        self.assertEqual(first['status']['type'], 'idle')
        original = len(self.sessions.conversation(TID)['messages'])
        with self.log.open('ab') as stream:
            stream.write(b'{"type":"user","message":{"content":"next')
        self.assertEqual(len(self.sessions.conversation(TID)['messages']), original)
        with self.log.open('ab') as stream:
            stream.write(b'"}}\n')
        self.assertEqual(len(self.sessions.conversation(TID)['messages']), original + 1)
        self.assertEqual(len(self.sessions.conversation(TID)['messages']), original + 1)
        self.assertFalse(self.sessions.conversation('../../elsewhere')['messages'])
    def test_external_open_session_is_observed_without_starting_cli(self):
        self.history()
        (self.root / 'sessions').mkdir()
        (self.root / 'sessions/777.json').write_text(json.dumps({'pid': 777, 'sessionId': TID}))
        self.control.submit(TID, 'attach')
        self.control.tick(self.rpc)
        task = self.control.snapshot()['tasks'][0]
        self.assertEqual(task['mode'], 'observe')
        self.assertIn('Claude Code', task['message'])
        self.assertFalse(self.rpc.running)
        self.control.submit(TID, 'message', 'must not send')
        self.control.tick(self.rpc)
        self.assertFalse(self.rpc.running)
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'failed')
    def test_resume_handshake_preserves_cwd_and_model_and_prompt_exactly_once(self):
        with patch.dict('os.environ', {'CLAUDECODE': '1'}):
            session = self.attach()
        command = session.process.command
        self.assertIn('--resume', command)
        self.assertIn(TID, command)
        self.assertNotIn('--model', command)
        self.assertNotIn('--dangerously-skip-permissions', command)
        self.assertEqual(session.process.options['cwd'], str(self.root))
        self.assertNotIn('CLAUDECODE', session.process.options['env'])
        self.control.submit(TID, 'message', 'run the task', 'same')
        self.control.submit(TID, 'message', 'run the task', 'same')
        self.control.tick(self.rpc)
        users = [e for e in session.process.sent if e['type'] == 'user']
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]['session_id'], TID)
        self.assertEqual(users[0]['message']['content'], 'run the task')
        self.control.submit(TID, 'message', 'new priority instruction', 'second')
        self.control.tick(self.rpc)
        users = [e for e in session.process.sent if e['type'] == 'user']
        self.assertEqual(len(users), 2)
        self.assertEqual(users[1]['priority'], 'now')
        self.assertEqual(users[1]['origin'], {'kind': 'human'})
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'succeeded')
        session.process.emit({'type': 'assistant', 'uuid': 'reply1', 'message': {'content': [{'type': 'text', 'text': 'reply text'}]}})
        session.process.emit({'type': 'result', 'is_error': False, 'user_message_uuids': [u['uuid'] for u in users]})
        self.until(lambda: session.active_turn is None)
        self.assertEqual(self.journal.db.execute("SELECT text FROM replies WHERE kind='agentMessage'").fetchone()[0], 'reply text')
    def test_permission_allow_deny_and_cancel_are_bound_to_session(self):
        session = self.attach()
        req = {'type': 'control_request', 'request_id': 'req1', 'request': {
            'subtype': 'can_use_tool', 'tool_name': 'Bash', 'input': {'command': 'git status'}}}
        session.dispatch(req)
        approval = self.approvals.public()[0]
        self.assertEqual(approval['client'], 'claude')
        self.assertEqual(approval['details']['threadId'], TID)
        self.assertEqual(self.approvals.decide(approval['token'], True), 'allowed')
        response = session.process.sent[-1]['response']['response']
        self.assertEqual(response, {'behavior': 'allow', 'updatedInput': {'command': 'git status'}})
        req['request_id'] = 'req2'
        session.dispatch(req)
        self.approvals.decide(self.approvals.public()[1]['token'], False)
        self.assertEqual(session.process.sent[-1]['response']['response']['behavior'], 'deny')
        req['request_id'] = 'req3'
        session.dispatch(req)
        session.dispatch({'type': 'control_cancel_request', 'request_id': 'req3'})
        self.assertEqual(self.approvals.public()[2]['decision'], 'resolved')
    def test_pause_interrupts_owned_turn_without_affecting_another_session(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'work')
        self.control.tick(self.rpc)
        self.control.submit(TID, 'pause')
        self.control.tick(self.rpc)
        self.until(lambda: session.active_turn is None)
        self.assertTrue(self.control.snapshot()['tasks'][0]['paused'])
        self.assertEqual([e['request']['subtype'] for e in session.process.sent if e['type'] == 'control_request'], ['initialize', 'interrupt'])
        self.control.submit(TID, 'message', 'blocked')
        self.control.tick(self.rpc)
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'failed')
    def test_pause_diagnostic_result_is_an_interruption_not_an_execution_error(self):
        session = self.attach()
        session.process.delay_interrupt_result = True
        self.control.submit(TID, 'message', 'work', 'pause-diagnostic-work')
        self.control.tick(self.rpc)
        sent = next(e for e in session.process.sent if e['type'] == 'user')
        self.control.submit(TID, 'pause', request_id='pause-diagnostic')
        self.control.tick(self.rpc)
        self.assertTrue(session.interrupting)
        self.assertTrue(self.control.snapshot()['tasks'][0]['paused'])
        session.dispatch({'type': 'result', 'is_error': True,
                          'errors': ['[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null'],
                          'user_message_uuid': sent['uuid']})
        self.assertIsNone(session.active_turn)
        self.assertFalse(session.interrupting)
        self.assertIsNone(session.last_error)
        self.assertFalse(any(isinstance(e['status'], dict) and e['status'].get('error')
                             for e in self.rpc.conversation(TID)['events']))
        status = self.journal.db.execute("SELECT status FROM task_events WHERE turn=? AND method='turn/completed'",
                                         ('pause-diagnostic-work',)).fetchone()[0]
        self.assertEqual(json.loads(status), 'interrupted')
        self.control.tick(self.rpc)
        task = self.control.snapshot()['tasks'][0]
        self.assertTrue(task['paused'])
        self.assertEqual(task['threadStatus']['type'], 'idle')
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)
        self.control.submit(TID, 'resume', request_id='continue-after-diagnostic')
        self.control.tick(self.rpc)
        self.assertFalse(self.control.snapshot()['tasks'][0]['paused'])
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'succeeded')

    def test_interjection_diagnostic_for_previous_turn_keeps_new_input_running(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'first work', 'diagnostic-first')
        self.control.tick(self.rpc)
        self.control.submit(TID, 'message', 'new direction', 'diagnostic-second')
        self.control.tick(self.rpc)
        users = [e for e in session.process.sent if e['type'] == 'user']
        session.dispatch({'type': 'result', 'is_error': True,
                          'result': '[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use',
                          'user_message_uuid': users[0]['uuid']})
        self.assertEqual(session.active_turn, 'diagnostic-second')
        self.assertFalse(any(isinstance(e['status'], dict) and e['status'].get('error')
                             for e in self.rpc.conversation(TID)['events']))
        session.dispatch({'type': 'result', 'is_error': False, 'result': 'new reply',
                          'user_message_uuid': users[1]['uuid']})
        self.assertIsNone(session.active_turn)
        self.assertIsNone(session.last_error)
        self.assertEqual(len(users), 2)

    def test_diagnostic_without_interrupt_intent_is_still_an_error(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'ordinary work', 'unexpected-diagnostic')
        self.control.tick(self.rpc)
        sent = next(e for e in session.process.sent if e['type'] == 'user')
        session.dispatch({'type': 'result', 'is_error': True,
                          'errors': ['[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null'],
                          'user_message_uuid': sent['uuid']})
        self.assertIn('ede_diagnostic', session.last_error)
        self.assertIn('ede_diagnostic', str(self.rpc.conversation(TID)['events']))

    def test_pause_does_not_hide_a_real_error_alongside_the_diagnostic(self):
        session = self.attach()
        session.process.delay_interrupt_result = True
        self.control.submit(TID, 'message', 'work', 'pause-real-failure')
        self.control.tick(self.rpc)
        sent = next(e for e in session.process.sent if e['type'] == 'user')
        self.control.submit(TID, 'pause', request_id='pause-before-real-failure')
        self.control.tick(self.rpc)
        session.dispatch({'type': 'result', 'is_error': True,
                          'result': '[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null',
                          'errors': ['Authentication failed'], 'user_message_uuid': sent['uuid']})
        self.assertIn('Authentication failed', session.last_error)
        self.assertIn('Authentication failed', str(self.rpc.conversation(TID)['events']))
        self.assertTrue(self.control.snapshot()['tasks'][0]['paused'])

    def test_pause_does_not_hide_a_diagnostic_for_a_different_turn(self):
        session = self.attach()
        session.process.delay_interrupt_result = True
        self.control.submit(TID, 'message', 'old work', 'unrelated-diagnostic')
        self.control.tick(self.rpc)
        first = next(e for e in session.process.sent if e['type'] == 'user')
        session.dispatch({'type': 'result', 'is_error': False, 'user_message_uuid': first['uuid']})
        self.control.submit(TID, 'message', 'current work', 'current-stopping-work')
        self.control.tick(self.rpc)
        self.control.submit(TID, 'pause', request_id='pause-current-only')
        self.control.tick(self.rpc)
        session.dispatch({'type': 'result', 'is_error': True,
                          'errors': ['[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null'],
                          'user_message_uuid': first['uuid']})
        self.assertIn('ede_diagnostic', str(self.rpc.conversation(TID)['events']))
        self.assertEqual(session.active_turn, 'current-stopping-work')
        self.assertTrue(session.interrupting)

    def test_pause_diagnostic_does_not_display_a_desktop_failure(self):
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtTest import QTest
            from PySide6.QtWidgets import QApplication, QMessageBox
            import monitor
            from desktop_qt import MainWindow
        except ImportError:
            self.skipTest('Qt desktop dependencies not installed')
        session = self.attach()
        session.process.delay_interrupt_result = True
        self.control.submit(TID, 'message', 'work', 'desktop-pause-work')
        self.control.tick(self.rpc)
        self.control.runtime[TID]['checkedAt'] = 0
        self.control.tick(self.rpc)
        sent = next(e for e in session.process.sent if e['type'] == 'user')
        original = monitor.snapshot()
        app = QApplication.instance() or QApplication([])
        window = None
        class Shared:
            def detach(self):pass
        class Workers:
            def join(self, timeout=None):pass
        try:
            with patch.object(monitor, 'controller', None), patch.object(monitor, 'claude_controller', self.control), \
                 patch.object(monitor, 'conversation_for', side_effect=lambda tid, client: self.rpc.conversation(tid) if client == 'claude' else {'messages': [], 'events': []}), \
                 patch.object(monitor, 'start_background_service', side_effect=lambda stop: (stop, Workers())):
                monitor.update(threads=[], controls={'tasks': [], 'operations': [], 'ready': True},
                               claude={'connection': 'connected', 'threads': [self.rpc.call('thread/read', {'threadId': TID})['thread']],
                                       'controls': self.control.snapshot()})
                window = MainWindow(Shared())
                window.timer.stop()
                window.show()
                window.switch_client('claude')
                QTest.qWait(30)
                QTest.mouseClick(window.task_action_buttons[TID]['pause'], Qt.MouseButton.LeftButton)
                self.control.tick(self.rpc)
                window.refresh()
                self.assertFalse(window.send_button.isEnabled())
                self.assertFalse(window.task_action_buttons[TID]['resume'].isEnabled())
                session.dispatch({'type': 'result', 'is_error': True,
                                  'errors': ['[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null'],
                                  'user_message_uuid': sent['uuid']})
                self.control.runtime[TID]['checkedAt'] = 0
                self.control.tick(self.rpc)
                window.refresh()
                QTest.qWait(30)
                self.assertFalse(any(dialog.isVisible() for dialog in window.findChildren(QMessageBox)))
                self.assertNotIn('执行未完成', window.conversation.toPlainText())
                self.assertNotIn('ede_diagnostic', window.conversation.toPlainText())
                self.assertTrue(window.task_action_buttons[TID]['resume'].isEnabled())
                self.assertTrue(self.control.snapshot()['tasks'][0]['paused'])
                self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)
        finally:
            if window is not None:
                window._quitting = True
                for dialog in window.findChildren(QMessageBox):dialog.reject()
                window.close()
                app.processEvents()
            monitor.update(**original)

    def test_new_test_task_needs_no_existing_selection_and_no_model_request(self):
        self.control.submit(None, 'create_test')
        self.control.tick(self.rpc)
        self.assertEqual(len(self.rpc.drafts), 1)
        self.assertFalse(self.rpc.running)
        self.assertFalse(self.control.snapshot()['tasks'][0]['managed'])
    def test_history_merges_live_reply_and_errors_without_duplicate_messages(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'new prompt', 'merge-turn')
        self.control.tick(self.rpc)
        session.dispatch({'type': 'assistant', 'uuid': 'merge-reply', 'message': {
            'content': [{'type': 'text', 'text': 'new reply'}]}})
        session.dispatch({'type': 'result', 'is_error': True, 'errors': ['test failure sk-secret123']})
        data = self.rpc.conversation(TID)
        self.assertEqual([m['text'] for m in data['messages']], ['existing task', 'done', 'new prompt', 'new reply'])
        self.assertIn('test failure', str(data['events']))
        self.assertNotIn('sk-secret123', str(data['events']))
        message_id = next(e['uuid'] for e in session.process.sent if e['type'] == 'user')
        self.append({'type': 'user', 'uuid': message_id, 'message': {'content': 'new prompt'}})
        self.append({'type': 'assistant', 'uuid': 'merge-reply', 'message': {
            'content': [{'type': 'text', 'text': 'new reply'}], 'stop_reason': 'end_turn'}})
        merged = self.rpc.conversation(TID)
        self.assertEqual(len([m for m in merged['messages'] if m['text'] == 'new prompt']), 1)
        self.assertEqual(len([m for m in merged['messages'] if m['text'] == 'new reply']), 1)
        self.assertIn('test failure', str(merged['events']))

    def test_new_external_window_revokes_existing_control_before_write(self):
        session = self.attach()
        (self.root / 'sessions').mkdir()
        path = self.root / 'sessions/777.json'
        path.write_text(json.dumps({'pid': 777, 'sessionId': TID, 'status': 'busy'}))
        with self.assertRaises(RPCError):
            self.rpc.call('turn/start', {'threadId': TID, 'input': [{'type': 'text', 'text': 'blocked'}]})
        self.control.submit(TID, 'message', 'blocked again', 'external-send')
        self.control.tick(self.rpc)
        task = self.control.snapshot()['tasks'][0]
        self.assertFalse(task['canControl'])
        self.assertEqual(task['mode'], 'observe')
        self.assertNotIn(TID, self.rpc.owned_threads)
        self.assertFalse([e for e in session.process.sent if e['type'] == 'user'])
        path.unlink()
        self.control.submit(TID, 'attach', request_id='reattach-after-close')
        self.control.tick(self.rpc)
        self.assertTrue(self.control.snapshot()['tasks'][0]['canControl'])
        self.control.submit(TID, 'message', 'accepted after release')
        self.control.tick(self.rpc)
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)

    def test_external_live_waiting_input_and_approval_states_are_preserved(self):
        self.history()
        (self.root / 'sessions').mkdir()
        path = self.root / 'sessions/777.json'
        for waiting, flag in [('input needed', 'waitingOnUserInput'), ('approval', 'waitingOnApproval')]:
            path.write_text(json.dumps({'pid': 777, 'sessionId': TID, 'status': 'waiting', 'waitingFor': waiting,
                                        'updatedAt': 1794649570651, 'statusUpdatedAt': 1794649570651}))
            thread = self.sessions.threads()[0]
            self.assertEqual(thread['status'], {'type': 'active', 'activeFlags': [flag]})
        path.write_text(json.dumps({'pid': 777, 'sessionId': TID, 'status': 'waiting', 'waitingFor': 'new unknown reason'}))
        self.assertEqual(self.sessions.threads()[0]['status'], {'type': 'unknown'})
        path.write_text(json.dumps({'pid': 12345, 'sessionId': TID, 'status': 'waiting', 'waitingFor': 'input needed'}))
        self.assertFalse(self.sessions.live())
        self.assertEqual(self.sessions.threads()[0]['status']['type'], 'idle')

    def test_continue_waits_for_interrupt_result_and_keeps_paused_until_accepted(self):
        session = self.attach()
        session.process.delay_interrupt_result = True
        self.control.submit(TID, 'message', 'work')
        self.control.tick(self.rpc)
        self.control.submit(TID, 'pause')
        self.control.tick(self.rpc)
        self.assertTrue(session.active_turn)
        self.control.submit(TID, 'resume', request_id='early-continue')
        self.control.tick(self.rpc)
        self.assertTrue(self.control.snapshot()['tasks'][0]['paused'])
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'failed')
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)
        session.dispatch({'type': 'result', 'is_error': False})
        self.control.submit(TID, 'resume', request_id='accepted-continue')
        self.control.tick(self.rpc)
        self.assertFalse(self.control.snapshot()['tasks'][0]['paused'])
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'succeeded')
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 2)

    def test_interjection_goes_to_same_running_process_before_previous_result(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'first job', 'first-live')
        self.control.tick(self.rpc)
        first = next(e for e in session.process.sent if e['type'] == 'user')
        session.dispatch({'type': 'command_lifecycle', 'command_uuid': first['uuid'], 'state': 'started'})
        self.control.submit(TID, 'message', 'do this now instead', 'insert-live')
        self.control.tick(self.rpc)
        users = [e for e in session.process.sent if e['type'] == 'user']
        self.assertEqual(len(users), 2)
        self.assertEqual(users[1]['priority'], 'now')
        self.assertIs(self.rpc.running[TID], session)
        self.assertEqual(session.active_turn, 'first-live')
        session.dispatch({'type': 'result', 'is_error': True, 'errors': ['Interrupted'], 'user_message_uuid': first['uuid']})
        self.assertEqual(session.active_turn, 'insert-live')
        self.assertFalse(session.last_error)
        session.dispatch({'type': 'command_lifecycle', 'command_uuid': users[1]['uuid'], 'state': 'started'})
        self.assertEqual(session.active_turn, 'insert-live')
        session.dispatch({'type': 'command_lifecycle', 'command_uuid': users[1]['uuid'], 'state': 'completed'})
        session.dispatch({'type': 'result', 'is_error': False, 'result': 'inserted reply', 'user_message_uuid': users[1]['uuid']})
        self.assertIsNone(session.active_turn)
        self.assertTrue(self.journal.db.execute("SELECT 1 FROM replies WHERE turn='insert-live' AND text='inserted reply'").fetchone())

    def test_pause_uses_session_stop_even_when_ui_turn_is_stale(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'work', 'active-job')
        self.control.tick(self.rpc)
        self.rpc.call('turn/interrupt', {'threadId': TID, 'turnId': 'stale-ui-turn'})
        self.until(lambda: session.active_turn is None)
        stop = next(e['request'] for e in session.process.sent if e.get('type') == 'control_request' and e['request']['subtype'] == 'interrupt')
        self.assertTrue(stop['cancel_queued'])
        self.assertTrue(stop['spare_background_tasks'])

    def test_pause_sweeps_pending_priority_messages_without_restarting_work(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'first', 'work-before-stop')
        self.control.tick(self.rpc)
        self.control.submit(TID, 'message', 'second', 'insert-before-stop')
        self.control.tick(self.rpc)
        self.control.submit(TID, 'pause', request_id='pause-inserted')
        self.control.tick(self.rpc)
        self.until(lambda: session.active_turn is None)
        self.assertFalse(session.inputs)
        self.assertTrue(self.control.snapshot()['tasks'][0]['paused'])
        self.control.tick(self.rpc)
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 2)

    def test_subagent_result_does_not_finish_main_turn(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'work', 'main-turn')
        self.control.tick(self.rpc)
        session.dispatch({'type': 'result', 'is_error': False, 'parent_tool_use_id': 'subagent'})
        self.assertEqual(session.active_turn, 'main-turn')

    def test_message_failure_after_write_is_not_retried(self):
        session = self.attach()
        original = session.process.write
        def uncertain(line):
            original(line)
            if json.loads(line).get('type') == 'user':
                raise OSError('lost confirmation')
        session.process.write = uncertain
        self.control.submit(TID, 'message', 'do not repeat', 'uncertain-user')
        self.control.tick(self.rpc)
        self.control.tick(self.rpc)
        self.assertEqual(self.control.snapshot()['operations'][0]['status'], 'unknown')
        self.assertEqual(len([e for e in session.process.sent if e['type'] == 'user']), 1)

    def test_stop_receipt_cannot_make_running_turn_look_finished(self):
        session = self.attach()
        session.process.delay_interrupt_result = True
        self.control.submit(TID, 'message', 'work', 'receipt-work')
        self.control.tick(self.rpc)
        sent = next(e for e in session.process.sent if e['type'] == 'user')
        self.rpc.call('turn/interrupt', {'threadId': TID, 'turnId': 'old-snapshot'})
        session.dispatch({'type': 'command_lifecycle', 'command_uuid': sent['uuid'], 'state': 'cancelled'})
        self.assertEqual(session.active_turn, 'receipt-work')
        self.assertTrue(session.interrupting)
        session.dispatch({'type': 'result', 'is_error': True, 'errors': ['Interrupted'],
                          'user_message_uuid': sent['uuid']})
        self.assertIsNone(session.active_turn)
        self.assertFalse(session.interrupting)
        self.assertFalse(session.last_error)

    def test_send_guard_rechecks_pause_inside_command_lock(self):
        session = self.attach()
        with self.assertRaises(RPCError):
            session.prompt('must not run after stop', 'guard-blocked', interject=True, guard=lambda: False)
        self.assertFalse([e for e in session.process.sent if e['type'] == 'user'])

    def test_real_execution_error_is_not_hidden_by_an_interjection(self):
        session = self.attach()
        self.control.submit(TID, 'message', 'work', 'actual-failure')
        self.control.tick(self.rpc)
        first = next(e for e in session.process.sent if e['type'] == 'user')
        self.control.submit(TID, 'message', 'new instruction', 'later-input')
        self.control.tick(self.rpc)
        session.dispatch({'type': 'result', 'is_error': True, 'errors': ['Authentication failed'],
                          'user_message_uuid': first['uuid']})
        self.assertIn('Authentication failed', str(self.rpc.conversation(TID)['events']))

    def test_unsent_draft_survives_restart_without_starting_model(self):
        path = self.root / 'drafts.json'
        rpc = ClaudeCodeRPC(sessions=self.sessions, command=['claude-test'], popen=FakeProcess, drafts_path=path)
        draft = rpc.call('thread/start', {'cwd': str(self.root)})['thread']
        rpc.call('thread/name/set', {'threadId': draft['id'], 'name': 'saved test task'})
        rpc.close()
        restored = ClaudeCodeRPC(sessions=self.sessions, command=['claude-test'], popen=FakeProcess, drafts_path=path)
        try:
            result = restored.call('thread/list', {})['data']
            self.assertEqual(result[0]['id'], draft['id'])
            self.assertEqual(result[0]['title'], 'saved test task')
            self.assertFalse(restored.running)
        finally:
            restored.close()

    def test_separate_client_views_never_mix_tasks_or_operations(self):
        import monitor
        data = {'connection': 'connected', 'threads': [{'id': TID, 'title': 'codex'}],
                'controls': {'operations': [{'id': 'codex-op'}]},
                'claude': {'connection': 'connected', 'threads': [{'id': TID, 'title': 'claude'}],
                           'controls': {'operations': [{'id': 'claude-op'}]}}}
        self.assertEqual(monitor.client_snapshot(data)['threads'][0]['title'], 'codex')
        self.assertEqual(monitor.client_snapshot(data, 'claude')['controls']['operations'][0]['id'], 'claude-op')
        with self.assertRaises(ValueError):
            monitor.get_controller('wrong')


if __name__ == '__main__':
    unittest.main()
