import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from codex_history import CodexHistory, read_codex_conversation
from reply_log import ReplyLog


THREAD = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


def line(kind, payload, second=0):
    return json.dumps(dict(type=kind, payload=payload, timestamp=f'2026-11-19T09:00:{second:02d}Z')).encode() + b'\n'


def message(turn, role, text, item, second=0, thread=THREAD):
    return line('event_msg', dict(type='item_completed', turn_id=turn, thread_id=thread,
                item=dict(type='AgentMessage' if role == 'assistant' else 'UserMessage', id=item,
                          content=[{'type': 'Text' if role == 'assistant' else 'text', 'text': text}])), second)


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.home / 'replies.sqlite3'
        self.log = ReplyLog(self.path)
        self.addCleanup(self.log.db.close)
        self.rollout = self.home / 'sessions' / '2026' / '11' / '19' / f'rollout-2026-11-19T09-00-00-{THREAD}.jsonl'
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_bytes(line('session_meta', {'id': THREAD}))
        self.enterContext(patch.dict('os.environ', CODEX_HOME=str(self.home)))

    def append(self, data):
        with self.rollout.open('ab') as stream:
            stream.write(data)

    def journal(self, turn, item, text, thread=THREAD):
        receipt = datetime.fromisoformat('2026-11-19T09:00:30Z').timestamp()
        with patch('reply_log.time.time', return_value=receipt):
            self.log.record(dict(method='item/completed', params=dict(threadId=thread, turnId=turn,
                            item=dict(id=item, type='agentMessage', text=text))), {thread})

    def messages(self):
        return [m['text'] for m in read_codex_conversation(self.path, THREAD)['messages']]

    def test_combines_terminal_and_app_messages_without_leaking_tool_or_context(self):
        self.append(line('response_item', dict(type='message', role='user', content=[{'text': 'hidden context'}])))
        self.append(line('event_msg', dict(type='exec_command_end', aggregated_output='private tool output')))
        self.append(line('event_msg', dict(type=['unexpected'])))
        self.append(message('one', 'user', 'terminal prompt', 'u', 1))
        self.append(message('one', 'assistant', 'terminal answer', 'a', 2))
        self.append(message('two', 'assistant', 'same item ID in a different turn', 'a', 3))
        self.append(message('two', 'assistant', 'child thread', 'a', 4, thread=OTHER))
        self.journal('two', 'a', 'same item ID in a different turn')
        self.journal('three', 'a', 'desktop reply pending flush')
        self.journal('three', 'a', 'unrelated session', thread=OTHER)
        self.assertEqual(self.messages(), ['terminal prompt', 'terminal answer', 'same item ID in a different turn',
                                           'desktop reply pending flush'])
        new = message('three', 'assistant', 'desktop reply pending flush', 'a', 5)
        self.append(new[:-1])
        self.assertEqual(self.messages().count('desktop reply pending flush'), 1)
        self.append(b'\n')
        self.assertEqual(self.messages().count('desktop reply pending flush'), 1)
        self.assertEqual(set(read_codex_conversation(self.path, THREAD)['messages'][0]), {'text', 'role', 'time', 'truncated'})

    def test_legacy_duplicates_and_rollback_use_turn_identity(self):
        self.append(line('event_msg', dict(type='task_started', turn_id='one'), 1))
        self.append(line('event_msg', dict(type='user_message', message='legacy prompt'), 2))
        self.append(line('event_msg', dict(type='agent_message', message='same legacy reply'), 3))
        self.append(line('event_msg', dict(type='agent_message', message='same legacy reply'), 4))
        self.journal('one', 'a1', 'same legacy reply')
        self.append(message('two', 'user', 'new prompt', 'u', 5))
        self.append(line('event_msg', dict(type='user_message', message='new prompt', turn_id='two'), 5))
        self.append(message('two', 'assistant', 'new reply', 'a', 6))
        self.journal('two', 'a', 'new reply')
        self.assertEqual(self.messages(), ['legacy prompt', 'same legacy reply', 'same legacy reply', 'new prompt', 'new reply'])
        self.append(line('event_msg', dict(type='thread_rolled_back', num_turns=1), 7))
        self.assertEqual(self.messages(), ['legacy prompt', 'same legacy reply', 'same legacy reply'])

    def test_reloads_replaced_rollout_and_checks_native_identity(self):
        self.append(message('one', 'user', 'before', 'a'))
        self.assertEqual(self.messages(), ['before'])
        new = self.rollout.with_suffix('.tmp')
        new.write_bytes(line('session_meta', {'id': THREAD}) + message('one', 'user', 'after', 'a'))
        new.replace(self.rollout)
        self.assertEqual(self.messages(), ['after'])
        self.rollout.write_bytes(line('session_meta', {'id': OTHER}) + message('one', 'user', 'wrong session', 'a'))
        self.assertEqual(self.messages(), [])

    def test_bounds_cold_io_and_only_consumes_appended_records(self):
        self.append(line('response_item', {'output': 'x' * 500}) * 12)
        self.append(message('one', 'user', 'old', 'before'))
        self.append(line('event_msg', {'type': 'tool', 'output': 'x' * 400}))
        self.append(message('one', 'user', 'recent', 'after'))
        with patch('codex_history.TAIL_LIMIT', 520), patch('codex_history.LINE_LIMIT', 400):
            reader = CodexHistory(self.home)
            completed, _, _ = reader.read(THREAD)
            self.assertEqual([m['text'] for m in completed], ['recent'])
            self.append(message('two', 'assistant', 'appended', 'after', 1))
            completed, _, _ = reader.read(THREAD)
            self.assertEqual([m['text'] for m in completed], ['recent', 'appended'])
