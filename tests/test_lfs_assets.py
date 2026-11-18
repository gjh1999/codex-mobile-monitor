import hashlib
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from handoff import HandoffError, git
from lfs_assets import HTTPSource, MEDIA, prefetch
from service_lock import ServiceLock


ASSET = '模型.lfs'
TWIN = 'spare.lfs'


class LFSFixture:
    def __init__(self, root, size=256 * 1024):
        self.root = root
        self.data = hashlib.shake_128(b'Lanxi LFS fixture').digest(size)
        self.oid = hashlib.sha256(self.data).hexdigest()
        self.pointer = f'version https://git-lfs.github.com/spec/v1\noid sha256:{self.oid}\nsize {size}\n'.encode()
        self.interrupt = 0
        self.bad_range = False
        self.ignore_range = False
        self.corrupt = False
        self.missing = False
        self.unsafe = False
        self.redirect = False
        self.requests = []
        self.batch_requests = []
        self.cache = root / '.git/lfs'
        self.part = self.cache / 'incomplete' / (self.oid + '.lanxi-part')
        self.object = self.cache / 'objects' / self.oid[:2] / self.oid[2:4] / self.oid
        (root / '.gitattributes').write_bytes(b'*.lfs filter=lfs diff=lfs merge=lfs -text\n')
        for name in (ASSET, TWIN):
            (root / name).write_bytes(self.pointer)
        (root / 'not-lfs.txt').write_bytes(self.pointer)
        git(root, 'init', '-q', isolated=True)
        git(root, 'add', '.', isolated=True)
        git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'pointers',
            isolated=True)
        self.commit = git(root, 'rev-parse', 'HEAD', isolated=True).decode().strip()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.handler())
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/models/demo.git/info/lfs'
        self.source = HTTPSource(self.endpoint, 'local-test', 'public-fixture-only')
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.worker.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                assert self.path == '/models/demo.git/info/lfs/objects/batch', self.path
                assert self.headers.get('Authorization', '').startswith('Basic ')
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert request['operation'] == 'download', request
                fixture.batch_requests.append(request['objects'])
                rows = []
                for entry in request['objects']:
                    row = dict(entry)
                    if fixture.missing or row['oid'] != fixture.oid:
                        row['error'] = {'code': 404}
                    else:
                        href = fixture.endpoint + '/objects/' + row['oid']
                        if fixture.unsafe:
                            href = href.replace('127.0.0.1', 'localhost')
                        row['actions'] = {'download': {'href': href, 'header': {'Authorization': 'Bearer fixture-media'}}}
                    rows.append(row)
                body = json.dumps({'transfer': 'basic', 'objects': rows}).encode()
                self.send_response(200)
                self.send_header('Content-Type', MEDIA)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                assert self.path.endswith('/objects/' + fixture.oid), self.path
                assert self.headers.get('Authorization') == 'Bearer fixture-media'
                range_header = self.headers.get('Range')
                start = int(range_header[6:-1]) if range_header and not fixture.ignore_range else 0
                if fixture.redirect:
                    fixture.requests.append((range_header, 0))
                    self.send_response(302)
                    self.send_header('Location', fixture.endpoint.replace('127.0.0.1', 'localhost') + '/never')
                    self.end_headers()
                    return
                data = fixture.data
                total = len(data)
                self.send_response(206 if start else 200)
                if start:
                    wrong = start + 1 if fixture.bad_range else start
                    self.send_header('Content-Range', f'bytes {wrong}-{total - 1}/{total}')
                self.send_header('Content-Length', str(total - start))
                self.end_headers()
                end = start + fixture.interrupt if fixture.interrupt else total
                fixture.requests.append((range_header, min(end, total) - start))
                payload = memoryview(data)[start:end]
                if fixture.corrupt:
                    payload = b'X' + bytes(payload[1:])
                try:
                    self.wfile.write(payload)
                    if fixture.interrupt:
                        self.connection.shutdown(socket.SHUT_WR)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    def fetch(self, source=True, paths=(ASSET,)):
        return prefetch(self.root, 'HEAD', paths, self.source if source else None)


class LFSTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = LFSFixture(Path(directory.name))
        self.addCleanup(self.fixture.close)

    def test_resume_deduplicates_and_reuses_verified_cache_without_network(self):
        f = self.fixture
        self.assertEqual(f.fetch(False)['files'][ASSET]['status'], 'missing_cache')
        self.assertFalse(f.cache.exists())
        f.interrupt = 65536
        first = f.fetch(paths=(ASSET, TWIN))
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['files'][ASSET]['receivedBytes'], f.interrupt)
        self.assertFalse(f.object.exists())
        self.assertEqual(f.part.stat().st_size, f.interrupt)
        f.bad_range = True
        self.assertEqual(f.fetch()['files'][ASSET]['status'], 'invalid_response')
        self.assertEqual(f.part.stat().st_size, f.interrupt)
        f.bad_range = False
        f.interrupt = 0
        result = f.fetch(paths=(ASSET, TWIN))
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['files'][ASSET]['resumedFrom'], 65536)
        self.assertEqual(f.requests[-1], ('bytes=65536-', len(f.data) - 65536))
        self.assertTrue(all(len(batch) == 1 for batch in f.batch_requests))
        self.assertEqual(f.object.read_bytes(), f.data)
        self.assertEqual((f.root / ASSET).read_bytes(), f.pointer)
        self.assertFalse(f.part.exists())
        self.assertEqual(f.fetch()['status'], 'ready')
        self.assertEqual(len(f.batch_requests), 3)

    def test_missing_and_corrupt_objects_never_appear_ready(self):
        f = self.fixture
        f.missing = True
        missing = f.fetch()
        self.assertEqual(missing['files'][ASSET]['status'], 'missing_remote')
        self.assertEqual(f.requests, [])
        f.missing = False
        f.corrupt = True
        corrupt = f.fetch()
        self.assertEqual(corrupt['files'][ASSET]['status'], 'corrupt_download')
        self.assertFalse(f.object.exists())
        self.assertFalse(f.part.exists())
        f.corrupt = False
        self.assertEqual(f.fetch()['status'], 'ready')
        f.object.write_bytes(b'X' + f.data[1:])
        self.assertEqual(f.fetch()['files'][ASSET]['status'], 'corrupt_cache')
        self.assertEqual(len(f.requests), 2)

    def test_server_that_ignores_range_restarts_instead_of_appending(self):
        f = self.fixture
        f.interrupt = 65536
        self.assertEqual(f.fetch()['status'], 'incomplete')
        f.interrupt = 0
        f.ignore_range = True
        result = f.fetch()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['files'][ASSET]['resumedFrom'], 0)
        self.assertEqual(f.object.read_bytes(), f.data)

    def test_blocks_cross_origin_and_redirects_and_never_uses_worktree_pointer(self):
        f = self.fixture
        f.unsafe = True
        (f.root / ASSET).write_bytes(b'unsaved local content')
        self.assertEqual(f.fetch()['files'][ASSET]['status'], 'unsafe_download')
        self.assertFalse(f.requests)
        f.unsafe = False
        f.redirect = True
        self.assertEqual(f.fetch()['files'][ASSET]['status'], 'http_error')
        self.assertEqual(len(f.requests), 1)
        self.assertEqual((f.root / ASSET).read_bytes(), b'unsaved local content')
        with self.assertRaisesRegex(HandoffError, '未对所选路径启用'):
            f.fetch(paths=('not-lfs.txt',))

    def test_concurrent_prefetch_does_not_open_a_second_download(self):
        f = self.fixture
        f.cache.mkdir()
        lock = ServiceLock(f.cache / 'lanxi-prefetch.lockfile')
        self.addCleanup(lock.close)
        self.assertEqual(f.fetch()['files'][ASSET]['status'], 'busy')
        self.assertEqual(f.batch_requests, [])

    def test_completed_partial_is_published_without_contacting_server(self):
        f = self.fixture
        f.part.parent.mkdir(parents=True)
        f.part.write_bytes(f.data)
        f.missing = True
        report = f.fetch()
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['files'][ASSET]['receivedBytes'], 0)
        self.assertEqual(f.batch_requests, [])
        self.assertTrue(f.object.exists())
