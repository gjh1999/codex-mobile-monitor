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

    def commit(self, root, path, content):
        (root / path).write_bytes(content)
        git(root, 'add', '--', path)
        git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'new commit')
        return git(root, 'rev-parse', 'HEAD').decode().strip()

    def handoff(self, include=(), base='HEAD'):
        package = self.root / 'sealed.zip'
        receipt = capture(self.sender, self.home, 'example', base, self.thread, include, package)
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

    def test_preserves_local_head_when_it_is_ahead(self):
        self.edit(self.sender, 16, b'incoming staged')
        git(self.sender, 'add', 'shared.txt')
        self.handoff()
        head = self.commit(self.local, 'local.txt', b'local commit\n')
        self.edit(self.local, 4, b'local working')
        script = Path(handoff_merge.__file__)
        result = subprocess.run([sys.executable, str(script), '--local', str(self.local), '--incoming',
                                 str(self.incoming), '--dest', str(self.dest)], capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['status'], report['head'], report['history']['mode']), ('merged', head, 'local_ahead'))
        self.assertIn(b'incoming staged', git(self.dest, 'show', ':shared.txt'))
        self.assertIn(b'local working', (self.dest / 'shared.txt').read_bytes())

    def test_fast_forwards_to_incoming_head(self):
        base = git(self.local, 'rev-parse', 'HEAD').decode().strip()
        head = self.commit(self.sender, 'incoming.txt', b'incoming commit\n')
        self.edit(self.local, 2, b'local staged')
        git(self.local, 'add', 'shared.txt')
        self.edit(self.sender, 26, b'incoming working')
        self.handoff(base=base)
        report = self.merge()
        self.assertEqual((report['head'], report['history']['mode'], report['history']['bases']),
                         (head, 'incoming_ahead', [base]))
        self.assertEqual(git(self.dest, 'show', 'HEAD:incoming.txt'), b'incoming commit\n')
        self.assertIn(b'local staged', git(self.dest, 'show', ':shared.txt'))
        self.assertIn(b'incoming working', (self.dest / 'shared.txt').read_bytes())
        self.assertEqual(prepare_resume(self.dest, self.root / 'target-home')['thread'], self.thread)

    def test_diverging_commits_preserve_both_parents_and_pending_changes(self):
        base = git(self.local, 'rev-parse', 'HEAD').decode().strip()
        left = self.commit(self.local, 'left.txt', b'left commit\n')
        right = self.commit(self.sender, 'right.txt', b'right commit\n')
        self.edit(self.local, 2, b'local staged')
        git(self.local, 'add', 'shared.txt')
        self.edit(self.local, 10, b'local working')
        self.edit(self.sender, 18, b'incoming staged')
        git(self.sender, 'add', 'shared.txt')
        self.edit(self.sender, 26, b'incoming working')
        self.handoff(base=base)
        self.assertEqual(git(self.incoming, 'rev-parse', 'HEAD').decode().strip(), right)
        indexes = [(root / '.git/index').read_bytes() for root in (self.local, self.incoming)]
        report = self.merge()
        self.assertEqual(report['history'], dict(mode='merge_commit', bases=[base], localHead=left, incomingHead=right))
        self.assertEqual(git(self.dest, 'rev-list', '--parents', '-1', 'HEAD').decode().split(),
                         [report['head'], left, right])
        self.assertEqual(git(self.dest, 'show', 'HEAD:left.txt'), b'left commit\n')
        self.assertEqual(git(self.dest, 'show', 'HEAD:right.txt'), b'right commit\n')
        self.assertNotIn(b'staged', git(self.dest, 'show', 'HEAD:shared.txt'))
        self.assertIn(b'local staged', git(self.dest, 'show', ':shared.txt'))
        self.assertIn(b'incoming staged', git(self.dest, 'show', ':shared.txt'))
        self.assertNotIn(b'working', git(self.dest, 'show', ':shared.txt'))
        self.assertIn(b'local working', (self.dest / 'shared.txt').read_bytes())
        self.assertIn(b'incoming working', (self.dest / 'shared.txt').read_bytes())
        self.assertEqual(indexes, [(root / '.git/index').read_bytes() for root in (self.local, self.incoming)])
        self.assertFalse((self.dest / '.git/objects/info/alternates').exists())
        self.assertEqual(prepare_resume(self.dest, self.root / 'target-home')['thread'], self.thread)

    def test_history_and_staged_overlay_conflicts_are_distinguished(self):
        base = git(self.local, 'rev-parse', 'HEAD').decode().strip()
        self.commit(self.sender, 'binary.dat', b'committed\x00remote')
        self.handoff(base=base)
        (self.local / 'binary.dat').write_bytes(b'staged\x00local')
        git(self.local, 'add', 'binary.dat')
        report = self.merge()
        self.assertEqual((report['status'], report['layer'], report['paths']), ('conflict', 'local_index', ['binary.dat']))
        self.assertFalse(self.dest.exists())
        git(self.local, 'commit', '-qm', 'conflicting commit')
        report = self.merge()
        self.assertEqual((report['status'], report['layer'], report['paths']), ('conflict', 'history', ['binary.dat']))
        self.assertFalse(self.dest.exists())

    def test_rejects_unrelated_and_shallow_history(self):
        self.handoff()
        unrelated = git(self.local, 'commit-tree', 'HEAD^{tree}', '-m', 'unrelated').decode().strip()
        git(self.local, 'update-ref', 'HEAD', unrelated)
        report = self.merge()
        self.assertEqual((report['status'], report['history']['reason']), ('history_unavailable', 'unrelated_history'))
        (self.local / '.git/shallow').write_bytes(unrelated.encode() + b'\n')
        report = self.merge()
        self.assertEqual(report['history']['reason'], 'shallow_history')
        self.assertFalse(self.dest.exists())

    def test_rejects_incompatible_object_formats(self):
        self.handoff()
        other = self.root / 'sha256'
        other.mkdir()
        git(other, 'init', '-q', '--object-format=sha256')
        self.commit(other, 'content.txt', b'sha256 fixture\n')
        report = prepare_merge(other, self.incoming, self.dest)
        self.assertEqual((report['status'], report['history']['reason']), ('history_unavailable', 'object_format'))
        self.assertFalse(self.dest.exists())

    def test_committed_private_paths_are_rejected(self):
        # Check locally committed paths too. Either input might have advanced since the shared baseline.
        self.handoff()
        self.commit(self.local, '.env', b'fixture only')
        with self.assertRaisesRegex(HandoffError, '私有'):
            self.merge()
        self.assertFalse(self.dest.exists())

    def test_case_collisions_between_commits_are_rejected(self):
        base = git(self.local, 'rev-parse', 'HEAD').decode().strip()
        self.commit(self.local, 'Case.txt', b'a')
        self.commit(self.sender, 'case.txt', b'b')
        self.handoff(base=base)
        with self.assertRaisesRegex(HandoffError, '大小写'):
            self.merge()
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
