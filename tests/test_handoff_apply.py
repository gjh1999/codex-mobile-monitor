import ctypes
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import handoff_apply
from coordination import Conflict, Ownership
from coordination_client import ControlDenied
from handoff import HandoffError, capture, git, handoff_receipts, native_publication_lock, prepare_resume
from tests import test_handoff_merge


class SimulatedCrash(BaseException):
    pass


@unittest.skipUnless(os.name == 'nt', 'Windows worktree switch')
class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_handoff_merge.MergeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.sender = self.fixture.local, self.fixture.sender
        self.home = self.fixture.root / 'local-home'
        self.original = git(self.root, 'rev-parse', 'HEAD').decode().strip()
        self.original_index = (self.root / '.git/index').read_bytes()
        self.original_shared = (self.root / 'shared.txt').read_bytes()
        self.center = Ownership(self.fixture.root / 'center.sqlite3')
        self.paths = {self.root: 'example'}
        self.active = self.root / '.git/lanxi-switch/active.json'

    def event(self, project, session, source, sequence, event):
        try:
            return self.center.report(project, 'local', session, source, sequence, event)
        except Conflict as error:
            raise ControlDenied(detail=str(error)) from error

    def prepared(self):
        (self.sender / 'newdir').mkdir()
        head = self.fixture.commit(self.sender, 'newdir/note.txt', b'peer commit\n')
        self.fixture.edit(self.sender, 2, b'staged test')
        git(self.sender, 'add', 'shared.txt')
        self.fixture.edit(self.sender, 20, b'working test')
        (self.sender / 'binary.dat').unlink()
        (self.sender / 'incoming.txt').write_bytes(b'untracked peer\r\n')
        self.fixture.handoff(['incoming.txt'], base=self.original)
        report = self.fixture.merge()
        self.assertEqual(report['status'], 'merged', report)
        return head

    def apply(self):
        return handoff_apply.switch(self, 'example', self.fixture.dest, self.home)

    def recover(self):
        return handoff_apply.switch(self, 'example', None, self.home)

    def assert_original(self):
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD').decode().strip(), self.original)
        self.assertEqual((self.root / '.git/index').read_bytes(), self.original_index)
        self.assertEqual((self.root / 'shared.txt').read_bytes(), self.original_shared)
        self.assertEqual((self.root / 'binary.dat').read_bytes(), b'old\x00data')
        for name in ('incoming.txt', 'newdir', '.git/index.lock', '.git/lanxi'):
            self.assertFalse((self.root / name).exists(), name)
        self.assertFalse(self.active.exists())
        self.assertEqual(self.center.status('example')['humans'], [])

    def test_applies_commit_and_preserves_local_metadata_and_files(self):
        head = self.prepared()
        git(self.root, 'branch', 'keep-local')
        git(self.root, 'config', 'user.name', 'Device account')
        git(self.root, 'config', 'remote.backup.url', 'ssh://reserved.invalid/fixture')
        (self.root / '.git/info/exclude').write_bytes(b'cache/\n')
        (self.root / 'cache').mkdir()
        (self.root / 'cache/private.bin').write_bytes(b'local only')
        (self.root / 'my-local.txt').write_bytes(b'untracked local')
        result = self.apply()
        self.assertEqual((result['status'], result['head'], result['files']), ('applied', head, 4))
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD').decode().strip(), head)
        self.assertEqual(git(self.root, 'rev-parse', 'keep-local').decode().strip(), self.original)
        self.assertEqual(git(self.root, 'config', '--get', 'user.name').strip(), b'Device account')
        self.assertEqual(git(self.root, 'config', '--get', 'remote.backup.url').strip(), b'ssh://reserved.invalid/fixture')
        self.assertIn(b'staged test', git(self.root, 'show', ':shared.txt'))
        self.assertNotIn(b'working test', git(self.root, 'show', ':shared.txt'))
        self.assertIn(b'working test', (self.root / 'shared.txt').read_bytes())
        self.assertFalse((self.root / 'binary.dat').exists())
        self.assertEqual((self.root / 'incoming.txt').read_bytes(), b'untracked peer\r\n')
        self.assertEqual((self.root / 'cache/private.bin').read_bytes(), b'local only')
        self.assertEqual((self.root / 'my-local.txt').read_bytes(), b'untracked local')
        self.assertEqual(Path(result['backup'], 'backup/shared.txt').read_bytes(), self.original_shared)
        self.assertEqual(prepare_resume(self.root, self.home)['thread'], self.fixture.thread)
        self.assertEqual(self.recover()['status'], 'no_pending_switch')

    def test_denies_occupied_dirty_and_colliding_paths(self):
        self.prepared()
        self.center.report('example', 'other', 'ide', 'vscode', 1, 'open')
        with self.assertRaises(ControlDenied):
            self.apply()
        self.center.report('example', 'other', 'ide', 'vscode', 2, 'close')
        self.fixture.edit(self.root, 10, b'own unsaved change')
        with self.assertRaisesRegex(HandoffError, '本机修改'):
            self.apply()
        (self.root / 'shared.txt').write_bytes(self.original_shared)
        (self.root / '.git/info/exclude').write_bytes(b'incoming.txt\n')
        (self.root / 'incoming.txt').write_bytes(b'private existing file')
        with self.assertRaisesRegex(HandoffError, '本机路径'):
            self.apply()
        self.assertEqual((self.root / 'incoming.txt').read_bytes(), b'private existing file')
        self.assertFalse(self.active.exists())

    def test_real_windows_file_in_use_rolls_back_other_paths(self):
        self.prepared()
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateFileW(str(self.root / 'shared.txt'), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            with self.assertRaises(PermissionError):
                self.apply()
            self.assert_original()
        finally:
            kernel.CloseHandle(handle)

    def test_file_and_index_changes_after_preflight_are_preserved(self):
        self.prepared()
        link = os.link
        install = handoff_apply.install_files
        index_lock = self.root / '.git/index.lock'

        def save_index_before_acquire(source, dest):
            if dest == index_lock:
                (self.root / 'late.txt').write_bytes(b'late staged edit\n')
                git(self.root, 'add', 'late.txt')
            return link(source, dest)

        with patch('handoff_apply.os.link', side_effect=save_index_before_acquire):
            with self.assertRaisesRegex(HandoffError, '索引在切换期间发生变化'):
                self.apply()
        self.assertEqual(git(self.root, 'show', ':late.txt'), b'late staged edit\n')
        git(self.root, 'restore', '--staged', '--', 'late.txt')

        def save_working_file(*args):
            self.fixture.edit(self.root, 12, b'kept late file edit')
            return install(*args)

        with patch('handoff_apply.install_files', side_effect=save_working_file):
            with self.assertRaisesRegex(HandoffError, '文件在切换期间发生变化'):
                self.apply()
        self.assertIn(b'kept late file edit', (self.root / 'shared.txt').read_bytes())
        self.assertEqual((self.root / 'binary.dat').read_bytes(), b'old\x00data')
        self.assertFalse(self.active.exists())
        self.assertFalse(index_lock.exists())
        self.assertEqual(self.center.status('example')['humans'], [])

    def test_recovery_after_crash_with_new_files_and_head_already_installed(self):
        self.prepared()
        write = handoff_apply.atomic_write

        def crash_after_install(path, data):
            if path == self.active and json.loads(data)['phase'] == 'applied':
                raise SimulatedCrash()
            return write(path, data)

        with patch('handoff_apply.atomic_write', side_effect=crash_after_install):
            with self.assertRaises(SimulatedCrash):
                self.apply()
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD').decode().strip(), self.original)
        self.assertEqual(self.center.status('example')['humans'][0]['source'], 'switch')
        self.assertEqual(handoff_receipts({'example': str(self.root)})[0]['status'], 'switch_pending')
        with self.assertRaisesRegex(HandoffError, '尚未结束'):
            prepare_resume(self.root, self.home)
        with self.assertRaisesRegex(HandoffError, '尚未结束'):
            capture(self.root, self.home, 'example', self.original, self.fixture.thread, [], self.root.parent / 'not-written.zip')
        self.assertEqual(self.recover()['status'], 'rolled_back')
        self.assert_original()

    def test_lost_close_acknowledgement_reconciles_an_applied_switch(self):
        head = self.prepared()
        event = self.event

        def lost_after_close(*args):
            state = event(*args)
            if args[-1] == 'close':
                raise ControlDenied('fixture lost close reply')
            return state

        with patch.object(self, 'event', side_effect=lost_after_close):
            with self.assertRaisesRegex(ControlDenied, 'lost close reply'):
                self.apply()
        self.assertTrue(self.active.is_file())
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD').decode().strip(), head)
        self.assertEqual(self.recover()['status'], 'applied')
        self.assertFalse(self.active.exists())
        self.assertEqual(prepare_resume(self.root, self.home)['thread'], self.fixture.thread)

    def test_native_writer_blocks_the_switch(self):
        self.prepared()
        self.home.mkdir()
        with native_publication_lock(self.home, self.fixture.thread):
            with self.assertRaisesRegex(HandoffError, '协调历史写入'):
                self.apply()
        self.assert_original()

    def test_new_worktree_with_old_local_history_cannot_be_published(self):
        incoming = self.fixture.rollout
        entries = [json.loads(line) for line in incoming.read_bytes().splitlines()]
        entries[0]['payload']['cwd'] = str(self.root)
        old = b''.join(json.dumps(data).encode() + b'\n' for data in entries)
        original = self.home / incoming.relative_to(self.fixture.home)
        original.parent.mkdir(parents=True)
        original.write_bytes(old)
        extra_turn = str(uuid.uuid4())
        extra = b''.join(json.dumps({'type': 'event_msg', 'payload': {'type': event, 'turn_id': extra_turn}}).encode() + b'\n'
                         for event in ('task_started', 'task_complete'))
        # Byte-prefix histories keep the original session metadata. The last
        # completed turn records the sender's current workspace.
        context = json.dumps({'type': 'turn_context', 'payload': {'cwd': str(self.sender)}}).encode() + b'\n'
        incoming.write_bytes(old + context + extra)
        self.prepared()
        self.apply()
        output = self.root.parent / 'unexpected.zip'
        with self.assertRaisesRegex(HandoffError, '先在原路径续接'):
            capture(self.root, self.home, 'example', self.original, self.fixture.thread, [], output)
        self.assertFalse(output.exists())
