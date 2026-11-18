import json
import os
import subprocess
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import handoff
from handoff import capture, git, HandoffError, prepare_resume, restore


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.a = self.root / 'A 源端'
        self.a.mkdir()
        git(self.a, 'init', '--quiet', '-b', 'feature')
        git(self.a, 'config', 'user.name', 'fixture')
        git(self.a, 'config', 'user.email', 'fixture@example.invalid')
        self.tracked = self.a / '示例.bin'
        self.tracked.write_bytes(b'base\x00')
        (self.a / 'remove.txt').write_bytes(b'remove\n')
        (self.a / '.gitignore').write_bytes(b'ignored.*\n')
        git(self.a, 'add', '.')
        git(self.a, 'commit', '-qm', 'shared baseline')
        self.seed = self.root / 'local clone'
        git(self.root, 'clone', '--quiet', '--no-local', self.a, self.seed)
        self.base = git(self.a, 'rev-parse', 'HEAD').decode().strip()
        (self.a / 'local.txt').write_bytes(b'unpushed\n')
        git(self.a, 'add', 'local.txt')
        git(self.a, 'commit', '-qm', 'only on A')
        self.tracked.write_bytes(b'index\x00')
        git(self.a, 'add', self.tracked.name)
        self.tracked.write_bytes(b'working\x00\r\n')
        (self.a / 'remove.txt').unlink()
        (self.a / 'added.txt').write_bytes(b'new\r\n')
        (self.a / 'ignored.key').write_bytes(b'private-fixture')
        (self.a / 'unselected.txt').write_bytes(b'private-fixture')
        self.thread, self.turn = str(uuid.uuid4()), str(uuid.uuid4())
        self.home_a, self.home_b = self.root / 'home A', self.root / 'home B'
        stamp = '2026-11-18T08:10:00.000Z'
        folder = self.home_a / 'sessions/2026/11/18'
        folder.mkdir(parents=True)
        self.log = folder / f'rollout-2026-11-18T08-10-00-{self.thread}.jsonl'
        events = [('session_meta', dict(id=self.thread, timestamp=stamp, cwd=str(self.a),
                                        originator='codex_cli_rs', cli_version='0.169.1', source='cli',
                                        model_provider='openai')),
                  ('event_msg', dict(type='task_started', turn_id=self.turn)),
                  ('event_msg', dict(type='user_message', message='fixture input', images=[])),
                  ('event_msg', dict(type='agent_message', message='fixture reply')),
                  ('event_msg', dict(type='task_complete', turn_id=self.turn, last_agent_message='fixture reply'))]
        self.log.write_bytes(b''.join((json.dumps(dict(timestamp=stamp, type=k, payload=v))
                                       + '\n').encode() for k, v in events))
        self.out = self.root / 'sealed.zip'
        self.dest = self.root / 'B 新目录'

    def pack(self, includes=('added.txt',)):
        return capture(self.a, self.home_a, 'alpha', self.base, self.thread, includes, self.out)['sha256']

    def unpack(self, digest):
        return restore(self.out, digest, 'alpha', self.seed, self.dest)

    def test_two_workspaces_and_session_homes(self):
        before = git(self.a, 'status', '--porcelain=v1', '-z')
        digest = self.pack()
        self.assertEqual(before, git(self.a, 'status', '--porcelain=v1', '-z'))
        result = self.unpack(digest)
        self.assertEqual((result['thread'], result['turn']), (self.thread, self.turn))
        self.assertEqual(git(self.a, 'rev-parse', 'HEAD'), git(self.dest, 'rev-parse', 'HEAD'))
        self.assertEqual(git(self.dest, 'branch', '--show-current'), b'feature\n')
        self.assertEqual(git(self.dest, 'show', ':示例.bin'), b'index\x00')
        self.assertEqual((self.dest / '示例.bin').read_bytes(), self.tracked.read_bytes())
        self.assertEqual((self.dest / 'added.txt').read_bytes(), b'new\r\n')
        self.assertFalse((self.dest / 'remove.txt').exists())
        self.assertFalse((self.dest / 'unselected.txt').exists())
        self.assertFalse((self.dest / 'ignored.key').exists())
        self.assertFalse(self.home_b.exists())
        self.assertEqual(prepare_resume(self.dest, self.home_b), dict(thread=self.thread, cwd=str(self.dest)))
        installed, = self.home_b.rglob('*.jsonl')
        self.assertEqual(installed.read_bytes(), self.log.read_bytes())
        with installed.open('ab') as file:
            file.write(b'{"fixture":"local continuation"}\n')
        self.assertEqual(prepare_resume(self.dest, self.home_b)['thread'], self.thread)
        self.assertTrue(installed.read_bytes().endswith(b'continuation"}\n'))
        self.assertIsNone(prepare_resume(self.seed, self.home_b))

    def test_bad_digest_wrong_project_existing_directory_and_retry(self):
        digest = self.pack()
        with self.assertRaises(HandoffError):
            self.unpack('0' * 64)
        with self.assertRaises(HandoffError):
            restore(self.out, digest, 'different', self.seed, self.dest)
        self.assertFalse(self.dest.exists())
        self.dest.mkdir()
        with self.assertRaises(HandoffError):
            self.unpack(digest)
        self.assertEqual(list(self.dest.iterdir()), [])
        with self.assertRaises(HandoffError):
            self.pack()
        self.dest.rmdir()
        self.unpack(digest)

    def test_active_partial_or_wrong_project_session(self):
        complete = self.log.read_bytes()
        for bad in (complete[:-1], complete.rsplit(b'\n', 2)[0] + b'\n',
                    complete + b'{"type":"event_msg","payload":{"type":"task_started","turn_id":"next"}}\n',
                    complete.replace(json.dumps(str(self.a)).encode(), b'"elsewhere"')):
            with self.subTest(bad=bad[-40:]), self.assertRaises(HandoffError):
                self.log.write_bytes(bad)
                self.pack()
            self.assertFalse(self.out.exists())

    def test_exclusions_and_concurrent_changes_leave_no_package(self):
        for name in ('ignored.key', 'ignored.txt', '../escape', '.git/config', '示例.bin', '.env'):
            with self.subTest(name=name), self.assertRaises(HandoffError):
                self.pack((name,))
        real = handoff.git_state
        calls = 0

        def capture_state(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.tracked.write_bytes(b'different content, identical status')
            return real(*args)

        with patch('handoff.git_state', side_effect=capture_state), self.assertRaises(HandoffError):
            self.pack()
        self.assertFalse(self.out.exists())

    def test_baseline_only_and_divergent_session(self):
        self.base = 'HEAD'
        result = self.pack()
        with zipfile.ZipFile(self.out) as package:
            self.assertNotIn('commits.bundle', package.namelist())
        with self.assertRaises(HandoffError):
            self.unpack(result)
        self.assertFalse(self.dest.exists())
        restore(self.out, result, 'alpha', self.a, self.dest)
        conflict = self.home_b / 'sessions' / self.log.name
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b'different thread history')
        with self.assertRaises(HandoffError):
            prepare_resume(self.dest, self.home_b)
        self.assertEqual(conflict.read_bytes(), b'different thread history')

    def test_changed_then_removed_private_file_in_unpushed_history(self):
        secret = self.a / '.env'
        secret.write_bytes(b'synthetic fixture')
        git(self.a, 'add', '.env')
        git(self.a, 'commit', '-qm', 'temporary private file')
        git(self.a, 'rm', '.env')
        git(self.a, 'commit', '-qm', 'remove private file')
        with self.assertRaisesRegex(HandoffError, '私有'):
            self.pack()
        self.assertFalse(self.out.exists())

    def test_turn_context_in_project_subdirectory_and_web_receipt(self):
        subdir = self.a / '子项目'
        subdir.mkdir()
        (subdir / 'file').touch()
        git(self.a, 'add', '子项目/file')
        lines = self.log.read_bytes().splitlines(keepends=True)
        lines.insert(-1, (json.dumps(dict(type='turn_context', payload={'cwd': str(subdir)})) + '\n').encode())
        self.log.write_bytes(b''.join(lines))
        digest = self.pack()
        self.unpack(digest)
        self.assertEqual(prepare_resume(self.dest, self.home_b)['cwd'], str(self.dest / '子项目'))
        receipts = handoff.handoff_receipts({'alpha': str(self.dest), 'missing': str(self.seed),
                                            'wrong': str(self.dest)})
        self.assertEqual([r['status'] for r in receipts], ['restored', 'awaiting_restore', 'invalid_receipt'])
        self.assertEqual(receipts[0]['sha256'], digest)

    @unittest.skipUnless(os.name == 'nt' and (Path(__file__).resolve().parents[1] /
                         '.venv/Scripts/python.exe').is_file(), 'requires the Windows terminal adapter')
    def test_bare_powershell_command_and_passthrough(self):
        self.unpack(self.pack())
        script = Path(__file__).resolve().parents[1] / 'scripts/lanxi-terminal.ps1'
        (self.root / 'codex.cmd').write_text('@echo off\necho argc1=[%~1] argc2=[%~2] argc3=[%~3] argc4=[%~4]\n',
                                            encoding='ascii')
        env = dict(os.environ, LANXI_TEST_SCRIPT=str(script), PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                   CODEX_HOME=str(self.home_b), LANXI_SEED=str(self.seed))
        cmd = '. $env:LANXI_TEST_SCRIPT; codex; codex --version; Set-Location -LiteralPath $env:LANXI_SEED; codex'
        proc = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                               '[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); ' + cmd],
                              cwd=self.dest, env=env, capture_output=True, encoding='utf-8', errors='replace',
                              timeout=30, check=True)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[0], f'argc1=[resume] argc2=[--cd] argc3=[{self.dest}] argc4=[{self.thread}]')
        self.assertEqual(lines[1], 'argc1=[--version] argc2=[] argc3=[] argc4=[]')
        self.assertEqual(lines[2], 'argc1=[] argc2=[] argc3=[] argc4=[]')
