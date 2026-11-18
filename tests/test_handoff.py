import json
import hmac
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import handoff
from coordination_client import ControlDenied, terminal
from handoff_sync import encode, sync_once
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

    def sync_fixture(self):
        left, right = self.root / 'shared-a', self.root / 'shared-b'
        left.mkdir()
        right.mkdir()
        self.exchange_a, self.exchange_b = left, right
        self.stage = self.root / 'staged'
        self.stage.mkdir()
        self.runtime_a, self.runtime_b = self.root / 'runtime-a', self.root / 'runtime-b'
        self.env = {'TEST_HANDOFF_KEY': 'fixture-private-key-' * 3}
        self.publisher = dict(device='office', projects={'alpha': dict(
            exchange=str(left), keyEnv='TEST_HANDOFF_KEY',
            publish=dict(repo=str(self.a), base=self.base, thread=self.thread, include=['added.txt', 'future.txt']),
        )})
        self.receiver = dict(device='laptop', projects={'alpha': dict(
            exchange=str(right), keyEnv='TEST_HANDOFF_KEY',
            receive=dict(seed=str(self.seed), staging=str(self.stage), peers=['office']),
        )})

    def ship_offer(self, data=None):
        source = self.exchange_a / 'alpha' / 'office.json'
        target = self.exchange_b / 'alpha' / 'office.json'
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(source.read_bytes() if data is None else data)
        return target

    def ship_package(self, digest, damaged=False):
        name = Path('alpha/objects') / (digest + '.zip')
        package = self.exchange_b / name
        package.parent.mkdir(exist_ok=True)
        if damaged:
            package.write_bytes(b'partial transfer')
        else:
            shutil.copyfile(self.exchange_a / name, package)

    def publish_auto(self):
        return sync_once(self.runtime_a, self.publisher, self.home_a, self.env)['projects']['alpha']['publish']

    def receive_auto(self):
        return sync_once(self.runtime_b, self.receiver, self.home_b, self.env)['projects']['alpha']['received'][0]

    def test_automatic_delivery_arrival_order_and_saved_edits(self):
        self.sync_fixture()
        source_before = git(self.a, 'status', '--porcelain', '-z')
        seed_before = git(self.seed, 'status', '--porcelain', '-z')
        first = self.publish_auto()['lastPublished']
        self.assertEqual(self.publish_auto()['lastPublished'], first)
        self.assertEqual(git(self.a, 'status', '--porcelain', '-z'), source_before)
        offer_path = self.ship_offer()
        initial_offer = offer_path.read_bytes()
        self.assertEqual(self.receive_auto()['status'], 'awaiting_package')
        self.ship_package(first['sha256'], damaged=True)
        self.assertEqual(self.receive_auto()['status'], 'restore_failed')
        self.assertEqual(list((self.stage / 'alpha').iterdir()), [])
        self.ship_package(first['sha256'])
        one = self.receive_auto()
        self.assertEqual(one['status'], 'ready')
        self.assertEqual(self.receive_auto(), one)
        self.assertFalse(self.home_b.exists())
        work = Path(one['cwd'])
        self.assertEqual((work / 'added.txt').read_bytes(), b'new\r\n')
        self.assertEqual(git(work, 'show', ':示例.bin'), b'index\x00')
        self.assertEqual((work / '示例.bin').read_bytes(), self.tracked.read_bytes())
        self.assertEqual(git(self.seed, 'status', '--porcelain', '-z'), seed_before)

        (work / 'added.txt').write_bytes(b'local work')
        (self.a / 'added.txt').write_bytes(b'new source edit in same turn')
        second = self.publish_auto()['lastPublished']
        self.assertEqual((first['revision'], second['revision']), (1, 2))
        self.assertEqual(first['turn'], second['turn'])
        self.ship_offer()
        self.ship_package(second['sha256'])
        two = self.receive_auto()
        self.assertEqual(two['status'], 'ready')
        self.assertNotEqual(one['cwd'], two['cwd'])
        self.assertEqual((Path(two['cwd']) / 'added.txt').read_bytes(), b'new source edit in same turn')
        self.assertEqual((work / 'added.txt').read_bytes(), b'local work')
        self.ship_offer(initial_offer)
        stale = self.receive_auto()
        self.assertEqual(stale['status'], 'version_conflict')
        self.assertEqual(stale['lastStaged']['revision'], 2)

    def test_automatic_verifies_signed_manifest_and_preserves_session_conflict(self):
        self.sync_fixture()
        first = self.publish_auto()['lastPublished']
        path = self.ship_offer()
        offer = json.loads(path.read_bytes())
        offer['revision'] = 999
        self.ship_offer(encode(offer))
        self.assertEqual(self.receive_auto()['status'], 'invalid_manifest')
        self.assertEqual(list(self.stage.iterdir()), [])

        offer.pop('signature')
        offer['turn'] = 'unrelated-turn'
        signed = {**offer, 'signature': hmac.digest(self.env['TEST_HANDOFF_KEY'].encode(), encode(offer), 'sha256').hex()}
        self.ship_offer(encode(signed))
        self.ship_package(first['sha256'])
        self.assertEqual(self.receive_auto()['status'], 'restore_failed')
        self.ship_offer()
        sessions = self.home_b / 'sessions'
        sessions.mkdir(parents=True)
        local_log = sessions / self.log.name
        local_log.write_bytes(b'existing divergent history')
        result = self.receive_auto()
        self.assertEqual(result['status'], 'session_conflict')
        self.assertTrue((Path(result['cwd']) / '.git/lanxi/session.jsonl').is_file())
        self.assertEqual(local_log.read_bytes(), b'existing divergent history')
        with self.assertRaises(HandoffError):
            prepare_resume(Path(result['cwd']), self.home_b)

    @unittest.skipUnless(os.name == 'nt', 'native rollout locking is verified on Windows')
    def test_failed_publication_preserves_independent_backup_and_can_retry(self):
        previous = self.log.read_bytes()
        next_turn = str(uuid.uuid4())
        events = [{'type': 'event_msg', 'payload': {'type': event, 'turn_id': next_turn}}
                  for event in ('task_started', 'task_complete')]
        transcript = previous + b''.join(json.dumps(event).encode() + b'\n' for event in events)
        self.log.write_bytes(transcript)
        self.unpack(self.pack())
        local = self.home_b / self.log.relative_to(self.home_a)
        local.parent.mkdir(parents=True)
        local.write_bytes(previous)

        self.assertEqual(handoff.session_progress(self.home_b, self.thread, transcript)[0], 'advance')
        with self.assertRaises(HandoffError):
            prepare_resume(self.dest, self.home_b)
        with patch('handoff.subprocess.run', return_value=Mock(stdout='codex-cli 0.170.0', returncode=0)):
            with self.assertRaisesRegex(HandoffError, '版本'):
                handoff.advance_session(self.home_b, self.thread, local, previous, transcript, 'codex.exe')
        version = Mock(stdout='codex-cli 0.169.1', returncode=0)
        with patch('handoff.subprocess.run', return_value=version):
            with patch.object(Path, 'replace', side_effect=PermissionError('fixture reader holds file')):
                with self.assertRaises(PermissionError):
                    handoff.advance_session(self.home_b, self.thread, local, previous, transcript, 'codex.exe')
        self.assertEqual(local.read_bytes(), previous)
        backup, = (self.home_b / 'lanxi-handoff-backups' / self.thread).glob('*.jsonl')
        local.write_bytes(previous + b'changed separately')
        self.assertEqual(backup.read_bytes(), previous)
        local.write_bytes(previous)
        with patch('handoff.subprocess.run', return_value=version):
            handoff.advance_session(self.home_b, self.thread, local, previous, transcript, 'codex.exe')
        self.assertEqual(local.read_bytes(), transcript)
        self.assertEqual(backup.read_bytes(), previous)
        compressed = local.with_suffix('.jsonl.zst')
        compressed.write_bytes(b'companion fixture')
        with self.assertRaisesRegex(HandoffError, '压缩'):
            handoff.session_progress(self.home_b, self.thread, transcript)

    def test_staged_terminal_uses_registered_thread_to_claim_project(self):
        self.unpack(self.pack())
        client = Mock(paths={self.seed: 'alpha'}, threads={})
        client.project_at.return_value = None
        client.event.return_value = {'writer': None}
        with self.assertRaises(ControlDenied) as error:
            terminal(client, self.dest, ['codex.exe'], resume=True)
        self.assertIn('登记', error.exception.detail)
        client.event.assert_not_called()

        client.threads = {self.thread: 'alpha'}
        receipt = handoff.resume_context(self.dest)
        with patch.dict(os.environ, {'CODEX_HOME': str(self.home_b)}), \
                patch('handoff.resume_context', return_value=receipt), \
                patch('coordination_client.subprocess.Popen', return_value=Mock(poll=lambda: 0, returncode=0)) as spawn:
            self.assertEqual(terminal(client, self.dest, ['codex.exe'], resume=True), 0)
        self.assertEqual([call.args[-1] for call in client.event.call_args_list], ['open', 'close'])
        self.assertEqual(client.event.call_args.args[0], 'alpha')
        spawn.assert_called_once_with(['codex.exe', 'resume', '--cd', str(self.dest), self.thread], cwd=self.dest)
        self.assertEqual(len(handoff.find_session(self.home_b, self.thread)), 1)

    def test_publisher_rejects_unregistered_checkout_in_staging_area(self):
        self.sync_fixture()
        unrelated = self.stage / 'alpha' / 'unrelated'
        unrelated.parent.mkdir()
        git(self.root, 'clone', '--quiet', self.a, unrelated)
        original = json.dumps(str(self.a)).encode()
        self.log.write_bytes(self.log.read_bytes().replace(original, json.dumps(str(unrelated)).encode()))
        self.publisher['projects']['alpha']['receive'] = {
            'staging': str(self.stage), 'seed': str(self.seed), 'peers': ['laptop'],
        }
        self.assertEqual(self.publish_auto()['status'], 'capture_blocked')
        self.assertFalse((self.exchange_a / 'alpha/office.json').exists())

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
