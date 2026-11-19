import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import uvicorn

from compute_queue import WINDOW, ComputeQueue
from compute_worker import LOG_LIMIT, WORKER_SCRIPT, Worker, record, result_report, worker_projects, write_record
from coordination import Ownership
from coordination_client import ControlClient, ControlDenied
from coordination_server import create_app
from handoff import git, sha


@unittest.skipUnless(os.name == 'nt' and shutil.which('git'), 'requires Windows and Git')
class WorkerTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = root / 'source'
        self.repo.mkdir()
        self.state = root / 'state'
        self.state.mkdir()
        git(self.repo, 'init', '-q', '--template=', isolated=True)
        (self.repo / 'sample.txt').write_bytes(b'pinned')
        (self.repo / 'task.py').write_text('''import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

assert 'LANXI_DEVICE_TOKEN' not in os.environ
if sys.argv[1] == 'slow':
    subprocess.Popen([sys.executable, '-I', '-c',
        "from pathlib import Path; import time; Path('ready').touch(); time.sleep(4); Path('escaped').touch()"])
    time.sleep(30)
else:
    result = hashlib.sha256(Path('sample.txt').read_bytes() * (2 ** 19)).hexdigest().encode()
    Path('result.txt').write_bytes(result)
    sys.stdout.buffer.write(b'x' * (2 * 1024**2 + 85))
''', encoding='ascii')
        git(self.repo, 'add', '.', isolated=True)
        git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com',
            'commit', '-qm', 'Pinned input', isolated=True)
        self.commit = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        (self.repo / 'sample.txt').write_bytes(b'dirty')

        self.database = root / 'center.sqlite3'
        self.now = 100.
        devices = {'office': dict(token='o' * 32, projects=['lab']),
                   'home': dict(token='h' * 32, projects=['lab'])}
        policy = dict(workers={'home': 'home'}, profiles=dict(run=dict(cpu=1, memoryMiB=128, vramMiB=0)))
        app = create_app(self.database, devices, lambda: self.now, compute={'lab': policy})
        endpoint = self.enterContext(socket.socket())
        endpoint.bind(('127.0.0.1', 0))
        options = dict(url=f'http://127.0.0.1:{endpoint.getsockname()[1]}', projects={'lab': {'path': str(self.repo)}})
        self.office = ControlClient(dict(options, device='office'), devices['office']['token'])
        self.home = ControlClient(dict(options, device='home'), devices['home']['token'])
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        server_thread = threading.Thread(target=lambda: self.server.run(sockets=[endpoint]), daemon=True)
        server_thread.start()
        self.addCleanup(self.stop_server, server_thread)
        deadline = time.monotonic() + 10
        while not self.server.started and server_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.server.started)
        self.office.event('lab', 'foreground', 'cli', 1, 'open')
        self.job = str(uuid.uuid4())
        self.office.request('lab', 'compute/jobs', dict(id=self.job, session='foreground', profile='run', commit=self.commit))
        self.attempt = self.state / 'attempts' / 'lab' / self.job / '1'

    def stop_server(self, thread):
        self.server.should_exit = True
        thread.join(10)

    def local_settings(self, mode='run', timeout=10):
        profile = dict(command=[sys.executable, '-I', 'task.py', mode], outputs=['result.txt'], timeoutSeconds=timeout)
        return dict(projects={'lab': dict(commits=[self.commit], profiles={'run': profile}, cpu=2, memoryMiB=512)})

    def worker(self, mode='run', timeout=10):
        projects = worker_projects(self.home, self.local_settings(mode, timeout), self.state)
        return Worker(self.home, projects, self.state)

    def start(self, worker):
        with patch.dict(os.environ, LANXI_DEVICE_TOKEN=self.home.token):
            self.assertEqual(worker.step(), 'starting')
        self.addCleanup(worker.supervisor.wait, timeout=15)

    def status(self):
        return self.office.request('lab', f'compute/jobs/{self.job}')

    def test_pins_input_caps_logs_and_retries_finish_after_restart(self):
        worker = self.worker()
        request = self.home.request

        def lost_start(project, endpoint, body=None):
            result = request(project, endpoint, body)
            if body and body.get('event') == 'start':
                raise ControlDenied(detail='simulated lost start response')
            return result

        with patch.object(self.home, 'request', side_effect=lost_start):
            with self.assertRaises(ControlDenied):
                worker.step()
        self.assertEqual(self.status()['status'], 'running')
        self.assertFalse((self.attempt / 'entered.json').exists())

        started = time.perf_counter()
        self.start(worker)
        spawned = time.perf_counter()
        self.assertEqual(worker.supervisor.wait(timeout=15), 0)
        completed = time.perf_counter()
        manifest = record(self.attempt / 'result.json')
        output = self.attempt / 'work' / 'result.txt'
        self.assertEqual(manifest['exitCode'], 0)
        self.assertEqual(output.read_bytes(), sha(b'pinned' * (2 ** 19)).encode())
        self.assertEqual((self.repo / 'sample.txt').read_bytes(), b'dirty')
        self.assertFalse((self.attempt / 'work' / '.git').exists())
        self.assertEqual(manifest['log']['bytes'], LOG_LIMIT)
        self.assertEqual(manifest['droppedLogBytes'], 85)
        saved = output.read_bytes()
        output.write_bytes(b'damaged')
        with self.assertRaises(ValueError):
            result_report(self.attempt, record(worker.active_file))
        output.write_bytes(saved)

        self.now += WINDOW + 1
        self.assertEqual(self.status()['status'], 'uncertain')

        def lost_finish(project, endpoint, body=None):
            result = request(project, endpoint, body)
            if body and body.get('event') == 'finish':
                raise ControlDenied(detail='simulated lost finish response')
            return result

        with patch.object(self.home, 'request', side_effect=lost_finish):
            with self.assertRaises(ControlDenied):
                worker.step()
        self.assertEqual(self.status()['status'], 'finished')
        resumed = self.worker()
        self.assertEqual(resumed.instance, worker.instance)
        report_started = time.perf_counter()
        self.assertEqual(resumed.step(), 'finished')
        reported = time.perf_counter()
        receipt = record(self.attempt / 'ack.json')
        self.assertEqual(receipt['resultSha256'], sha((self.attempt / 'result.json').read_bytes()))
        self.assertEqual(receipt['resultSha256'], self.status()['resultSha256'])
        self.assertIsNone(resumed.supervisor)
        self.assertFalse(resumed.active_file.exists())
        if os.getenv('LANXI_MEASURE_COMPUTE_WORKER'):
            print(f'worker_ms resume_start={(spawned-started)*1000:.2f} supervisor={(completed-spawned)*1000:.2f}'
                  f' report_retry={(reported-report_started)*1000:.2f}', flush=True)

    def test_timeout_stops_descendant_before_reporting(self):
        worker = self.worker(mode='slow', timeout=3)
        self.start(worker)
        self.assertEqual(worker.supervisor.wait(timeout=15), 0)
        self.assertEqual(worker.step(), 'finished')
        manifest = record(self.attempt / 'result.json')
        self.assertEqual((manifest['exitCode'], manifest['outcome']), (124, 'timed_out'))
        self.assertTrue((self.attempt / 'work' / 'ready').exists())
        time.sleep(2)
        self.assertFalse((self.attempt / 'work' / 'escaped').exists())

    def test_does_not_replay_an_entered_attempt_without_result(self):
        worker = self.worker()
        active = worker.claim()
        self.assertEqual(worker.event(active, 'start')['status'], 'running')
        active['phase'] = 'authorized'
        write_record(worker.active_file, active)
        self.attempt.mkdir(parents=True)
        write_record(self.attempt / 'entered.json', {'pid': 0})
        self.now += WINDOW + 1
        self.assertEqual(worker.step(), 'uncertain')
        self.assertIsNone(worker.supervisor)
        ComputeQueue(Ownership(self.database, lambda: self.now), {}).confirm_stopped('lab', self.job, 1, 'home')
        self.assertEqual(worker.step(), 'released')
        self.assertEqual(worker.step(), 'idle')

    def test_releases_a_claim_that_was_reassigned_before_start(self):
        worker = self.worker()
        active = worker.claim()
        self.assertEqual(active['attempt'], 1)
        self.now += WINDOW + 1
        replacement_state = self.state / 'replacement'
        replacement_state.mkdir()
        replacement = Worker(self.home, worker.projects, replacement_state)
        self.assertEqual(replacement.claim()['attempt'], 2)
        self.assertEqual(worker.step(), 'released')
        self.assertIsNone(worker.supervisor)

    def test_cli_process_reports_after_the_claiming_process_exits(self):
        config = self.state.parent / 'worker.json'
        write_record(config, dict(coordination=dict(device='home', url=self.home.url,
                     projects={'lab': {'path': str(self.repo)}}), computeWorker=self.local_settings()))
        command = [sys.executable, str(WORKER_SCRIPT), '--config', str(config), '--state-dir', str(self.state), '--once']
        env = dict(os.environ, LANXI_DEVICE_TOKEN=self.home.token)
        attempt_started = subprocess.check_output(command, env=env, timeout=15)
        self.assertIn(b'"starting"', attempt_started)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            outcome = subprocess.check_output(command, env=env, timeout=10)
            if b'"finished"' in outcome:
                break
            self.assertTrue(b'"running"' in outcome or b'"starting"' in outcome, outcome)
            time.sleep(0.05)
        self.assertEqual(self.status()['status'], 'finished')
        self.assertTrue((self.attempt / 'ack.json').exists())
