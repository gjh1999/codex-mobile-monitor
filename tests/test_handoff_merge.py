import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import handoff_merge
from handoff import HandoffError, capture, git, prepare_resume, restore
from handoff_merge import prepare_merge


class MergeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.local, self.sender, self.incoming, self.dest = [self.root / name for name in
                                                             ('local', 'sender', 'incoming', 'merged')]
        self.local.mkdir()
        git(self.local, 'init', '-q', '-b', 'main')
        git(self.local, 'config', 'user.name', 'Lanxi Fixture')
        git(self.local, 'config', 'user.email', 'fixture@example.invalid')
        lines = ''.join(f'Line {i:02d}\n' for i in range(32)).encode()
        (self.local / 'shared.txt').write_bytes(lines)
        (self.local / 'binary.dat').write_bytes(b'old\x00data')
        (self.local / '.gitattributes').write_bytes(b'shared.txt merge=fixture\n')
        git(self.local, 'add', '.')
        git(self.local, 'commit', '-qm', 'shared')
        git(self.root, 'clone', '--quiet', '--no-local', self.local, self.sender)
        self.thread, self.turn = str(uuid.uuid4()), str(uuid.uuid4())
        self.home = self.root / 'codex'
        folder = self.home / 'sessions/2026/11/18'
        folder.mkdir(parents=True)
        self.rollout = folder / f'rollout-2026-11-18T10-00-00-{self.thread}.jsonl'
        messages = [
            {'type': 'session_meta', 'payload': {'id': self.thread, 'cwd': str(self.sender)}},
            *[{'type': 'event_msg', 'payload': {'type': event, 'turn_id': self.turn}}
              for event in ('task_started', 'task_complete')],
        ]
        self.rollout.write_bytes(b''.join(json.dumps(data).encode() + b'\n' for data in messages))

    def edit(self, root, line, text):
        file = root / 'shared.txt'
        data = file.read_bytes()
        file.write_bytes(data.replace(f'Line {line:02d}'.encode(), text))

    def handoff(self, include=()):
        package = self.root / 'sealed.zip'
        receipt = capture(self.sender, self.home, 'example', 'HEAD', self.thread, include, package)
        restore(package, receipt['sha256'], 'example', self.local, self.incoming)

    def merge(self, includes=()):
        return prepare_merge(self.local, self.incoming, self.dest, includes)

    def test_independent_staged_and_unstaged_edits_and_safe_new_files(self):
        self.edit(self.local, 2, b'local staged')
        git(self.local, 'add', 'shared.txt')
        self.edit(self.local, 10, b'local working')
        self.edit(self.sender, 18, b'remote staged')
        git(self.sender, 'add', 'shared.txt')
        self.edit(self.sender, 26, b'remote working')
        (self.sender / 'peer notes.txt').write_bytes(b'peer\r\n')
        (self.local / 'my notes.txt').write_bytes(b'mine\r\n')
        (self.local / '.env').write_bytes(b'fixture-only-secret')
        self.handoff(['peer notes.txt'])
        before = [git(path, 'status', '--porcelain', '-z') for path in (self.local, self.incoming)]
        original_indexes = [(path / '.git/index').read_bytes() for path in (self.local, self.incoming)]
        custom_driver = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'merge.fixture.driver',
                         'GIT_CONFIG_VALUE_0': 'exit 9'}
        with patch.dict(os.environ, custom_driver):
            result = self.merge(['my notes.txt'])
        self.assertEqual(result['status'], 'merged')
        disk = (self.dest / 'shared.txt').read_bytes()
        staged = git(self.dest, 'show', ':shared.txt')
        for text in (b'local staged', b'remote staged'):
            self.assertIn(text, disk)
            self.assertIn(text, staged)
        for text in (b'local working', b'remote working'):
            self.assertIn(text, disk)
            self.assertNotIn(text, staged)
        self.assertEqual((self.dest / 'my notes.txt').read_bytes(), b'mine\r\n')
        self.assertEqual((self.dest / 'peer notes.txt').read_bytes(), b'peer\r\n')
        self.assertFalse((self.dest / '.env').exists())
        self.assertEqual(original_indexes, [(path / '.git/index').read_bytes() for path in (self.local, self.incoming)])
        self.assertEqual(before, [git(path, 'status', '--porcelain', '-z') for path in (self.local, self.incoming)])
        self.assertFalse((self.dest / '.git/objects/info/alternates').exists())
        self.assertEqual(prepare_resume(self.dest, self.root / 'target-home'),
                         {'cwd': str(self.dest), 'thread': self.thread})

    def test_reports_text_conflict_and_keeps_inputs(self):
        self.edit(self.local, 4, b'local conflicting line')
        self.edit(self.sender, 4, b'remote conflicting line')
        self.handoff()
        result = self.merge()
        self.assertEqual((result['status'], result['layer'], result['paths']), ('conflict', 'worktree', ['shared.txt']))
        self.assertTrue(any('CONFLICT' in message['type'] for message in result['messages']))
        self.assertFalse(self.dest.exists())
        self.assertIn(b'local conflicting line', (self.local / 'shared.txt').read_bytes())
        self.assertIn(b'remote conflicting line', (self.incoming / 'shared.txt').read_bytes())

    def test_different_commit_rejected_before_creating_result(self):
        self.handoff()
        git(self.local, 'commit', '--allow-empty', '-qm', 'new base')
        script = Path(handoff_merge.__file__)
        result = subprocess.run([sys.executable, str(script), '--local', str(self.local), '--incoming',
                                 str(self.incoming), '--dest', str(self.dest)], capture_output=True, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'different_head')
        self.assertFalse(self.dest.exists())

    def test_invalid_new_file_and_conflicting_staged_binary(self):
        (self.local / 'binary.dat').write_bytes(b'local\x00')
        git(self.local, 'add', 'binary.dat')
        (self.sender / 'binary.dat').write_bytes(b'remote\x00')
        git(self.sender, 'add', 'binary.dat')
        self.handoff()
        (self.local / '.env').write_bytes(b'fixture')
        with self.assertRaisesRegex(HandoffError, '私有'):
            self.merge(['.env'])
        result = self.merge()
        self.assertEqual((result['status'], result['layer'], result['paths']), ('conflict', 'index', ['binary.dat']))
        self.assertFalse(self.dest.exists())

    def test_combined_attributes_cannot_enable_a_checkout_filter(self):
        attributes = self.local / '.gitattributes'
        attributes.write_bytes(attributes.read_bytes() + b'*.asset filter=fixture\n')
        git(self.local, 'add', '.gitattributes')
        (self.sender / 'extra.asset').write_bytes(b'unfiltered fixture')
        self.handoff(['extra.asset'])
        with self.assertRaisesRegex(HandoffError, '过滤器'):
            self.merge()
        self.assertFalse(self.dest.exists())

    def test_input_changed_during_merge_is_not_published(self):
        self.handoff()
        read = handoff_merge.capture_index

        def change_before_recheck(root, temporary, includes, write_objects):
            if not write_objects and root == self.local:
                self.edit(self.local, 5, b'user saved during merge')
            return read(root, temporary, includes, write_objects)

        with patch('handoff_merge.capture_index', side_effect=change_before_recheck):
            with self.assertRaisesRegex(HandoffError, '发生变化'):
                self.merge()
        self.assertFalse(self.dest.exists())
        self.assertIn(b'user saved during merge', (self.local / 'shared.txt').read_bytes())
