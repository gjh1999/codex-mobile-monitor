import concurrent.futures
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from compute_queue import CANCEL_EXIT_CODE, ComputeQueue
from coordination import Conflict, Ownership
from coordination_server import create_app


COMMIT = '1' * 40
OTHER = '2' * 40


class ComputeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / 'center.sqlite3'
        self.now = 100.
        self.devices = {name: {'token': name[0] * 32, 'projects': ['vision', 'other']}
                        for name in ('office', 'home', 'laptop')}
        self.policy = dict(workers={'home': 'home', 'office': 'local'}, profiles={
            'train': dict(cpu=2, memoryMiB=2048, vramMiB=4096),
            'render': dict(cpu=2, memoryMiB=1024, vramMiB=0),
        })
        self.instances = {name: str(uuid.uuid4()) for name in self.devices}
        self.api = self.server()
        self.assertEqual(self.request('office', 'activity', dict(session='front', source='cli', sequence=1,
                                                                 event='open')).status_code, 200)

    def server(self):
        client = TestClient(create_app(self.database, self.devices, lambda: self.now,
                                       compute={'vision': self.policy, 'other': self.policy}))
        self.addCleanup(client.close)
        return client

    def request(self, device, endpoint, payload=None, project='vision'):
        headers = dict(Authorization='Bearer ' + self.devices[device]['token'], **{'X-Lanxi-Device': device})
        url = f'/api/v1/projects/{project}/{endpoint}'
        return self.api.get(url, headers=headers) if payload is None else self.api.post(url, json=payload, headers=headers)

    def heartbeat(self, device, vram=(8192,), commits=(COMMIT,), project='vision', **changes):
        body = dict(instance=self.instances[device], profiles=['train', 'render'], commits=list(commits),
                    cpu=8, memoryMiB=8192, gpuVramMiB=list(vram))
        return self.request(device, 'compute/heartbeat', {**body, **changes}, project)

    def submit_body(self, profile='train', **changes):
        return {**dict(id=str(uuid.uuid4()), session='front', profile=profile, commit=COMMIT), **changes}

    def submit(self, profile='train', **changes):
        payload = self.submit_body(profile, **changes)
        response = self.request('office', 'compute/jobs', payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def claim(self, device, project='vision'):
        response = self.request(device, 'compute/claim', dict(instance=self.instances[device]), project)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['job']

    def transition(self, device, job, event, **fields):
        payload = dict(instance=self.instances[device], ticket=job['ticket'], event=event)
        return self.request(device, f"compute/jobs/{job['id']}/events", {**payload, **fields})

    def test_pins_inputs_binds_foreground_and_hides_worker_credentials(self):
        body = self.submit_body()
        self.assertEqual(self.request('laptop', 'compute/jobs', body).status_code, 409)
        self.assertEqual(self.request('laptop', 'compute/claim', {'instance': self.instances['laptop']}).status_code, 403)
        self.assertEqual(self.request('office', 'compute/jobs', {**body, 'command': 'echo demo'}).status_code, 422)
        initial = self.request('office', 'compute/jobs', body)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(self.request('office', 'compute/jobs', {**body, 'commit': OTHER}).status_code, 409)
        self.now += 33
        self.assertEqual(self.request('office', 'compute/jobs', self.submit_body()).status_code, 409)
        self.assertEqual(initial.json(), self.request('office', 'compute/jobs', body).json())
        self.heartbeat('home')
        claimed = self.claim('home')
        status = self.request('office', f"compute/jobs/{body['id']}").json()
        self.assertEqual((status['status'], status['commit']), ('claimed', COMMIT))
        self.assertNotIn('ticket', status)
        self.assertEqual(claimed, self.claim('home'))
        self.assertEqual(self.request('laptop', f"compute/jobs/{body['id']}").status_code, 404)

    def test_prefers_local_cpu_and_home_gpu_with_matching_inputs(self):
        cpu = self.submit('render')
        gpu = self.submit('train')
        self.heartbeat('office')
        self.heartbeat('home', vram=(2048, 2048))
        self.assertIsNone(self.claim('home'))
        local = self.claim('office')
        self.assertEqual((local['id'], local['route']), (cpu['id'], 'local_capacity'))
        self.heartbeat('home', commits=(OTHER,))
        self.assertIsNone(self.claim('home'))
        self.heartbeat('home')
        home = self.claim('home')
        self.assertEqual((home['id'], home['route']), (gpu['id'], 'home_gpu'))

    def test_offline_and_busy_home_allow_local_fallback_without_gpu_overcommit(self):
        one = self.submit()
        two = self.submit()
        self.heartbeat('home')
        self.assertEqual(self.claim('home')['id'], one['id'])
        self.heartbeat('office', vram=(4095,))
        self.assertIsNone(self.claim('office'))
        self.heartbeat('office', vram=(4096,))
        self.assertEqual(self.claim('office')['id'], two['id'])

    def test_unstarted_expiry_requeues_and_old_claim_is_fenced(self):
        job = self.submit()
        self.heartbeat('home')
        old = self.claim('home')
        self.now += 31
        self.heartbeat('office')
        new = self.claim('office')
        self.assertEqual((new['id'], new['attempt'], new['route']), (job['id'], 2, 'local_capacity'))
        self.assertNotEqual(new['ticket'], old['ticket'])
        self.assertEqual(self.transition('home', old, 'start').status_code, 409)

    def test_disconnect_and_restart_preserve_running_job_and_single_result(self):
        self.submit()
        self.submit()
        self.heartbeat('home')
        job = self.claim('home')
        started = self.transition('home', job, 'start')
        self.assertEqual(started.json()['status'], 'running')
        self.assertEqual(self.transition('home', job, 'start').json(), started.json())
        self.now += 20
        self.assertEqual(self.transition('home', job, 'renew').status_code, 200)
        self.now += 20
        self.api = self.server()
        self.assertEqual(self.request('office', f"compute/jobs/{job['id']}").json()['status'], 'running')
        self.now += 11
        self.heartbeat('home')
        self.assertEqual(self.claim('home')['status'], 'uncertain')
        self.assertEqual(self.transition('home', job, 'start').status_code, 409)
        self.assertEqual(self.transition('home', job, 'finish', exitCode=0).status_code, 422)
        fields = dict(exitCode=0, resultSha256='9' * 64)
        finished = self.transition('home', job, 'finish', **fields)
        self.assertEqual(finished.json()['status'], 'finished')
        self.assertEqual(finished.json(), self.transition('home', job, 'finish', **fields).json())
        self.assertEqual(self.transition('home', job, 'finish', **dict(fields, exitCode=1)).status_code, 409)
        self.assertNotEqual(self.claim('home')['id'], job['id'])

    def test_racing_claims_and_projects_share_one_worker_slot(self):
        job = self.submit()
        self.heartbeat('home')
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            claims = list(pool.map(lambda _: self.claim('home'), range(4)))
        self.assertEqual({(row['id'], row['ticket'], row['attempt']) for row in claims},
                         {(job['id'], claims[0]['ticket'], 1)})
        self.heartbeat('home', project='other')
        self.assertIsNone(self.claim('home', project='other'))

    def test_verified_operator_recovery_releases_slot_without_replaying(self):
        self.submit()
        pending = self.submit()
        self.heartbeat('home')
        job = self.claim('home')
        self.assertEqual(self.transition('home', job, 'start').status_code, 200)
        recovery = ComputeQueue(Ownership(self.database, lambda: self.now), {})
        with self.assertRaises(Conflict):
            recovery.confirm_stopped('vision', job['id'], job['attempt'], 'home')
        self.now += 31
        with self.assertRaises(Conflict):
            recovery.confirm_stopped('vision', job['id'], job['attempt'] + 1, 'home')
        stopped = recovery.confirm_stopped('vision', job['id'], job['attempt'], 'home')
        self.assertEqual(stopped['status'], 'abandoned')
        self.assertEqual(self.transition('home', job, 'finish', exitCode=0, resultSha256='9' * 64).status_code, 409)
        self.heartbeat('home')
        self.assertEqual(self.claim('home')['id'], pending['id'])

    def test_only_submitter_can_cancel_queued_or_unstarted_work(self):
        queued = self.submit()
        endpoint = f"compute/jobs/{queued['id']}/cancel"
        self.assertEqual(self.request('laptop', endpoint, {}).status_code, 404)
        self.assertEqual(self.request('office', endpoint, {}, project='other').status_code, 404)
        canceled = self.request('office', endpoint, {}).json()
        self.assertEqual((canceled['status'], canceled['attempt'], canceled['cancelRequestedAt']), ('canceled', 0, self.now))
        self.now += 1
        self.assertEqual(self.request('office', endpoint, {}).json(), canceled)
        self.heartbeat('home')
        self.assertIsNone(self.claim('home'))

        claimed = self.submit()
        lease = self.claim('home')
        endpoint = f"compute/jobs/{claimed['id']}/cancel"
        self.assertEqual(self.request('home', endpoint, {}).status_code, 404)
        self.assertEqual(self.request('office', endpoint, {}).json()['status'], 'canceled')
        self.assertEqual(self.transition('home', lease, 'start').status_code, 409)
        pending = self.submit()
        self.assertEqual(self.claim('home')['id'], pending['id'])

    def test_cancellation_holds_the_slot_until_authenticated_stop_receipt(self):
        running = self.submit()
        pending = self.submit()
        self.heartbeat('home')
        lease = self.claim('home')
        self.assertEqual(self.transition('home', lease, 'start').status_code, 200)
        receipt = dict(exitCode=CANCEL_EXIT_CODE, resultSha256='9' * 64)
        self.assertEqual(self.transition('home', lease, 'canceled', **receipt).status_code, 409)
        endpoint = f"compute/jobs/{running['id']}/cancel"
        requested = self.request('office', endpoint, {}).json()
        self.assertEqual((requested['status'], requested['cancelRequestedAt']), ('running', self.now))
        self.now += 31
        self.api = self.server()
        self.heartbeat('home')
        self.assertEqual(self.claim('home')['id'], running['id'])
        offline = self.transition('home', lease, 'renew').json()
        self.assertEqual((offline['status'], offline['cancelRequestedAt']), ('uncertain', requested['cancelRequestedAt']))
        self.assertEqual(self.transition('laptop', lease, 'canceled', **receipt).status_code, 403)
        self.assertEqual(self.transition('home', dict(lease, ticket='x' * 43), 'canceled', **receipt).status_code, 409)
        self.assertEqual(self.transition('home', lease, 'canceled', **dict(receipt, exitCode=0)).status_code, 409)
        stopped = self.transition('home', lease, 'canceled', **receipt).json()
        self.assertEqual((stopped['status'], stopped['resultSha256']), ('canceled', receipt['resultSha256']))
        self.assertEqual(self.transition('home', lease, 'canceled', **receipt).json(), stopped)
        self.assertEqual(self.transition('home', lease, 'canceled', **dict(receipt, resultSha256='8' * 64)).status_code, 409)
        self.assertEqual(self.request('office', endpoint, {}).json(), stopped)
        self.assertEqual(self.claim('home')['id'], pending['id'])

    def test_migrated_running_job_can_finish_even_if_stop_was_requested(self):
        job = self.submit()
        self.heartbeat('home')
        lease = self.claim('home')
        self.assertEqual(self.transition('home', lease, 'start').status_code, 200)
        with Ownership(self.database).transaction() as db:
            db.execute('ALTER TABLE compute_jobs DROP COLUMN cancel_requested')
        self.api = self.server()
        endpoint = f"compute/jobs/{job['id']}/cancel"
        self.assertEqual(self.request('office', endpoint, {}).json()['status'], 'running')
        finished = self.transition('home', lease, 'finish', exitCode=0, resultSha256='f' * 64).json()
        self.assertEqual(finished['status'], 'finished')
        self.assertEqual(self.request('office', endpoint, {}).json(), finished)

    def test_cli_submits_and_reads_through_the_loopback_service(self):
        app = create_app(self.database, self.devices, lambda: self.now, compute={'vision': self.policy})
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        root = self.database.parent
        with socket.socket() as endpoint:
            endpoint.bind(('127.0.0.1', 0))
            config = root / 'device.json'
            config.write_text(json.dumps(dict(coordination=dict(url=f'http://127.0.0.1:{endpoint.getsockname()[1]}',
                              device='office', projects={'vision': {'path': str(root)}}))), encoding='utf-8')
            worker = threading.Thread(target=lambda: server.run(sockets=[endpoint]), daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.025)
                self.assertTrue(server.started)
                body = root / 'submit.json'
                payload = self.submit_body('render')
                body.write_text(json.dumps(payload), encoding='utf-8')
                cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / 'compute_client.py'),
                       '--config', str(config), '--project', 'vision']
                env = dict(os.environ, LANXI_DEVICE_TOKEN=self.devices['office']['token'])
                submitted = json.loads(subprocess.check_output([*cmd, 'submit', '--body', str(body)], env=env, timeout=10))
                status = json.loads(subprocess.check_output([*cmd, 'status', payload['id']], env=env, timeout=10))
                self.assertEqual(submitted, status)
                self.assertEqual((status['profile'], status['status']), ('render', 'queued'))
            finally:
                server.should_exit = True
                worker.join(5)
