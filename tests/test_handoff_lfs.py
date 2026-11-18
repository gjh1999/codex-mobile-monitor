import json
import os
import unittest
import zipfile
from pathlib import Path

from handoff import HandoffError, git, prepare_resume
from handoff_sync import sync_once
from lfs_assets import cache_object, matches
from tests import test_handoff_auto
from tests.test_lfs_assets import ASSET, LFSFixture


@unittest.skipUnless(os.name == 'nt', 'Windows original-path switch')
class HandoffLFSTests(unittest.TestCase):
    media_size = 256 * 1024

    def setUp(self):
        auto = test_handoff_auto.AutoApplyTests()
        auto.setUp()
        self.addCleanup(auto.doCleanups)
        self.auto = auto
        self.fixture = auto.fixture
        path = self.fixture.root / 'lfs-media'
        path.mkdir()
        self.media = LFSFixture(path, self.media_size)
        self.addCleanup(self.media.close)
        f = self.fixture
        f.env.update(LFS_USER='local-test', LFS_TOKEN='public-fixture-only')
        f.publisher['projects']['alpha']['publish']['lfs'] = [ASSET]
        (f.a / '.gitattributes').write_bytes(b'*.lfs filter=lfs diff=lfs merge=lfs -text\n')
        (f.a / ASSET).write_bytes(self.media.pointer)
        git(f.a, 'add', '--', '.gitattributes', ASSET, isolated=True)
        git(f.a, 'commit', '-qm', 'add LFS pointer', '--', '.gitattributes', ASSET, isolated=True)
        (f.a / ASSET).write_bytes(self.media.data)
        self.memo = {}

    def enable_prefetch(self, paths=None):
        self.auto.config['lfs'] = dict(paths=[ASSET] if paths is None else paths, endpoint=self.media.endpoint,
                                      userEnv='LFS_USER', tokenEnv='LFS_TOKEN')

    def scan(self):
        f = self.fixture
        return sync_once(f.runtime_b, f.receiver, f.home_b, f.env, control=self.auto, lfs_memo=self.memo)['projects']['alpha']

    def test_signed_pointer_waits_for_verified_cache_then_switches_once(self):
        f, media = self.fixture, self.media
        offer = self.auto.ship()
        self.assertEqual(offer['lfs'], {ASSET: [media.oid, len(media.data)]})
        bundle = f.exchange_b / 'alpha/objects' / (offer['sha256'] + '.zip')
        with zipfile.ZipFile(bundle) as package:
            manifest = json.loads(package.read('manifest.json'))
            self.assertEqual(manifest['lfs'], offer['lfs'])
            self.assertLess(sum(member.file_size for member in package.infolist()), len(media.data))
        result = self.scan()
        self.assertEqual(result['received'][0]['lfs'][ASSET]['status'], 'missing_cache')
        self.assertEqual(result['autoApply']['status'], 'lfs_pending')
        ready_path = Path(result['received'][0]['cwd'])
        self.assertTrue(ready_path.is_dir())
        self.assertFalse(f.home_b.exists())
        with self.assertRaisesRegex(HandoffError, '对象缺失'):
            prepare_resume(ready_path, f.home_b)

        self.enable_prefetch(paths=[])
        self.assertEqual(self.scan()['autoApply']['status'], 'lfs_unconfigured')
        self.assertEqual(media.batch_requests, [])
        self.enable_prefetch()
        media.missing = True
        blocked = self.scan()
        self.assertEqual(blocked['received'][0]['lfs'][ASSET]['status'], 'missing_remote')
        self.assertEqual(blocked['autoApply']['status'], 'lfs_pending')
        media.missing = False
        media.corrupt = True
        blocked = self.scan()
        self.assertEqual(blocked['received'][0]['lfs'][ASSET]['status'], 'corrupt_download')
        self.assertEqual(blocked['autoApply']['status'], 'lfs_pending')
        self.auto.assert_unmoved()
        self.assertEqual(self.auto.reads, 0)

        media.corrupt = False
        media.interrupt = 65536
        self.assertEqual(self.scan()['autoApply']['status'], 'lfs_pending')
        media.interrupt = 0
        ready = self.scan()
        self.assertEqual(ready['received'][0]['status'], 'ready')
        self.assertEqual(ready['autoApply']['status'], 'applied', ready)
        self.assertEqual(media.requests[-1], ('bytes=65536-', len(media.data) - 65536))
        self.assertEqual(ready['received'][0]['cwd'], str(ready_path))
        self.assertEqual((f.seed / ASSET).read_bytes(), media.pointer)
        native = cache_object(f.seed, media.oid)
        self.assertTrue(matches(native, media.oid, len(media.data)))
        self.assertEqual(prepare_resume(f.seed, f.home_b)['thread'], f.thread)
        self.assertEqual(self.scan()['autoApply']['status'], 'applied')
        native.unlink()
        self.assertEqual(self.scan()['autoApply']['status'], 'lfs_pending')
        with self.assertRaisesRegex(HandoffError, '对象缺失'):
            prepare_resume(f.seed, f.home_b)

    def test_working_or_staged_lfs_edits_cannot_be_sealed(self):
        f, media = self.fixture, self.media
        original = self.auto.ship()
        (f.a / ASSET).write_bytes(b'X' + media.data[1:])
        blocked = f.publish_auto()
        self.assertEqual(blocked['status'], 'capture_blocked')
        self.assertEqual(blocked['lastPublished'], original)
        alternate = media.pointer.replace(media.oid.encode(), b'a' * 64)
        (f.a / ASSET).write_bytes(alternate)
        git(f.a, 'add', '--', ASSET, isolated=True)
        self.assertEqual(f.publish_auto()['status'], 'capture_blocked')
        self.assertEqual((f.a / ASSET).read_bytes(), alternate)

    def test_new_revisions_share_assets_and_modified_receipts_cannot_request_objects(self):
        f, media = self.fixture, self.media
        self.auto.config.pop('autoApply')
        self.auto.ship()
        self.enable_prefetch()
        first = self.scan()['received'][0]
        self.assertEqual(first['status'], 'ready')
        self.assertEqual(len(media.requests), 1)
        (f.a / 'added.txt').write_bytes(b'second edition with same required object')
        self.auto.ship()
        second = self.scan()['received'][0]
        self.assertEqual(second['status'], 'ready')
        self.assertEqual(len(media.requests), 1)
        self.assertNotEqual(first['cwd'], second['cwd'])
        first_cache = cache_object(Path(first['cwd']), media.oid)
        second_cache = cache_object(Path(second['cwd']), media.oid)
        self.assertTrue(first_cache.samefile(second_cache))
        marker = Path(second['cwd']) / '.git/lanxi/handoff.json'
        data = json.loads(marker.read_bytes())
        data['lfs'][ASSET][0] = 'b' * 64
        marker.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.scan()['received'][0]['status'], 'staging_conflict')
        self.assertEqual(len(media.batch_requests), 1)
