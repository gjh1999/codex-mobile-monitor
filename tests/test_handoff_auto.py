import json
import os
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from coordination import Conflict, Ownership
from coordination_client import ControlDenied
from handoff import HandoffError, capture, git, native_publication_lock, prepare_resume
from handoff_sync import state_path, sync_once
from tests import test_handoff


@unittest.skipUnless(os.name == 'nt', 'Windows worktree switch')
class AutoApplyTests(unittest.TestCase):
    device = 'laptop'
    token = 'fixture-control-token'

    def setUp(self):
        fixture = test_handoff.HandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.sync_fixture()
        self.fixture = fixture
        self.paths, self.threads = {fixture.seed: 'alpha'}, {fixture.thread: 'alpha'}
        self.config = fixture.receiver['projects']['alpha']['receive']
        self.config['autoApply'] = dict(peer='office', thread=fixture.thread, branch='feature')
        self.center = Ownership(fixture.root / 'center.sqlite3')
        self.base = git(fixture.seed, 'rev-parse', 'HEAD').decode().strip()
        self.reads = 0

    def request(self, project, endpoint):
        self.assertEqual(endpoint, 'control')
        self.reads += 1
        return self.center.status(project)

    def event(self, project, session, source, sequence, event):
        try:
            return self.center.report(project, self.device, session, source, sequence, event)
        except Conflict as error:
            raise ControlDenied(code=409, detail=str(error)) from error

    def ship(self):
        offer = self.fixture.publish_auto()['lastPublished']
        self.fixture.ship_offer()
        self.fixture.ship_package(offer['sha256'])
        return offer

    def scan(self):
        f = self.fixture
        return sync_once(f.runtime_b, f.receiver, f.home_b, f.env, control=self)['projects']['alpha']

    def assert_unmoved(self):
        root = self.fixture.seed
        self.assertEqual(git(root, 'rev-parse', 'HEAD').decode().strip(), self.base)
        self.assertFalse((root / 'added.txt').exists())
        self.assertTrue((root / 'remove.txt').exists())

    def test_idle_apply_and_replay_skip_git_and_coordinator(self):
        f = self.fixture
        (f.seed / 'ignored.cache').write_bytes(b'device data')
        offer = self.ship()
        row = self.scan()
        applied = row['autoApply']
        self.assertEqual((applied['status'], applied['head'], applied['lastApplied']['sha256']),
                         ('applied', offer['head'], offer['sha256']))
        self.assertEqual(git(f.seed, 'show', ':示例.bin'), b'index\x00')
        self.assertEqual((f.seed / '示例.bin').read_bytes(), b'working\x00\r\n')
        self.assertEqual((f.seed / 'ignored.cache').read_bytes(), b'device data')
        self.assertTrue(Path(row['received'][0]['cwd']).is_dir())
        with patch('handoff_auto.clean') as clean, patch('handoff_auto.prepare_merge') as merge:
            self.assertEqual(self.scan()['autoApply'], applied)
            state_path(f.runtime_b).unlink()
            self.assertEqual(self.scan()['autoApply'], applied)
        clean.assert_not_called()
        merge.assert_not_called()
        self.assertEqual(self.reads, 1)
        self.assertEqual(prepare_resume(f.seed, f.home_b)['thread'], f.thread)
        with self.assertRaisesRegex(HandoffError, '完成新轮次'):
            capture(f.seed, f.home_b, 'alpha', f.base, f.thread, ['added.txt'], f.out, publish=True)

    def test_occupied_dirty_wrong_branch_and_edited_stage_are_deferred(self):
        f = self.fixture
        self.ship()
        self.center.report('alpha', 'laptop', 'editor', 'vscode', 1, 'open')
        row = self.scan()
        self.assertEqual(row['autoApply']['status'], 'occupied')
        self.assert_unmoved()
        stage = Path(row['received'][0]['cwd'])
        self.center.report('alpha', 'laptop', 'editor', 'vscode', 2, 'close')
        (f.seed / '示例.bin').write_bytes(b'device changes')
        self.assertEqual(self.scan()['autoApply']['status'], 'local_changes')
        self.assertEqual((f.seed / '示例.bin').read_bytes(), b'device changes')
        (f.seed / '示例.bin').write_bytes(b'base\x00')
        self.config['autoApply']['branch'] = 'other'
        self.assertEqual(self.scan()['autoApply']['status'], 'branch_mismatch')
        self.config['autoApply']['branch'] = 'feature'
        (stage / 'added.txt').write_bytes(b'edited after restore')
        changed = self.scan()['autoApply']
        self.assertEqual(changed['status'], 'staging_changed')
        self.assert_unmoved()
        (stage / 'added.txt').write_bytes(b'new\r\n')
        reads = self.reads
        self.assertEqual(self.scan()['autoApply'], changed)
        self.assertEqual(self.reads, reads)
        with patch('handoff_auto.time.time', return_value=changed['retryAt'] + 1):
            self.assertEqual(self.scan()['autoApply']['status'], 'applied')

    def test_existing_newer_local_session_blocks_an_older_snapshot(self):
        f = self.fixture
        offer = self.ship()
        local = f.home_b / f.log.relative_to(f.home_a)
        local.parent.mkdir(parents=True)
        turn = str(uuid.uuid4())
        data = f.log.read_bytes() + b''.join(json.dumps({'type': 'event_msg', 'payload': {
            'type': event, 'turn_id': turn}}).encode() + b'\n' for event in ('task_started', 'task_complete'))
        local.write_bytes(data)
        row = self.scan()
        self.assertEqual(row['autoApply']['status'], 'local_session_ahead')
        self.assertEqual(row['received'][0]['offer'], offer)
        self.assertEqual(local.read_bytes(), data)
        self.assert_unmoved()

    def test_new_offer_after_merge_aborts_before_touching_original(self):
        f = self.fixture
        one = self.ship()
        (f.a / 'added.txt').write_bytes(b'new signed edition')
        two = f.publish_auto()['lastPublished']
        self.assertEqual(two['revision'], one['revision'] + 1)

        @contextmanager
        def replace_offer(home, thread):
            with native_publication_lock(home, thread):
                f.ship_offer()
                yield

        with patch('handoff_apply.native_publication_lock', replace_offer):
            self.assertEqual(self.scan()['autoApply']['status'], 'deferred')
        self.assert_unmoved()
        self.assertEqual(self.center.status('alpha')['humans'], [])
        self.assertEqual(self.scan()['autoApply']['status'], 'awaiting_package')
        f.ship_package(two['sha256'])
        self.assertEqual(self.scan()['autoApply']['status'], 'applied')
        self.assertEqual((f.seed / 'added.txt').read_bytes(), b'new signed edition')

    def test_unsigned_offer_and_pending_recovery_never_reserve_project(self):
        self.ship()
        f = self.fixture
        damaged = json.loads((f.exchange_b / 'alpha/office.json').read_bytes())
        damaged['revision'] += 1
        f.ship_offer(json.dumps(damaged).encode())
        self.assertEqual(self.scan()['autoApply']['status'], 'invalid_manifest')
        f.ship_offer()
        active = f.seed / '.git/lanxi-switch/active.json'
        active.parent.mkdir()
        active.write_bytes(b'fixture pending recovery')
        self.assertEqual(self.scan()['autoApply']['status'], 'recovery_required')
        self.assert_unmoved()
        self.assertEqual(self.reads, 0)
