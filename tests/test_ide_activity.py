import queue
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch
from uuid import uuid4

import uvicorn

from coordination import Ownership
from coordination_client import ControlClient, ControlDenied, PersonalTaskLocks
from coordination_server import create_app
from ide_activity import close_queue, enqueue_close, flush_closes, report
from personal_sync import PersonalSync


class IDEActivityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.center = Ownership(self.root / 'center.sqlite3')
        self.outbox = self.root / 'outbox' / 'close.sqlite3'
        self.events = []
        self.offline = False
        self.lose_open_reply = False
        self.device = 'workstation'
        self.url = 'http://localhost'
        self.paths = {self.root: 'vision'}

    def event(self, project, session, source, sequence, event):
        if self.offline:
            raise ControlDenied()
        state = self.center.report(project, self.device, session, source, sequence, event)
        self.events.append((event, state))
        if self.lose_open_reply and event == 'open':
            self.lose_open_reply = False
            self.offline = True
            raise ControlDenied()
        return state

    def run_window(self, command, source='vscode'):
        commands = queue.Queue()
        commands.put(command)
        report(self, self.outbox, 'vision', source, commands)

    def test_clean_close_and_crash_have_distinct_outcomes(self):
        self.run_window('close')
        self.assertEqual([name for name, _ in self.events], ['open', 'close'])
        self.assertEqual(self.center.status('vision')['humans'], [])
        self.run_window('', source='pycharm')
        humans = self.center.status('vision')['humans']
        self.assertEqual([entry['source'] for entry in humans], ['pycharm'])
        self.assertEqual(len(self.events), 3)

    def test_offline_clean_close_replays_after_lost_open_ack(self):
        self.lose_open_reply = True
        self.run_window('close')
        self.assertEqual(len(self.center.status('vision')['humans']), 1)
        self.offline = False
        flush_closes(self.outbox, self)
        flush_closes(self.outbox, self)
        self.assertEqual([name for name, _ in self.events], ['open', 'close'])
        self.assertEqual(self.center.status('vision')['humans'], [])
        with closing(sqlite3.connect(self.outbox)) as db:
            rows = db.execute('SELECT count(*) FROM pending_close').fetchone()
        self.assertEqual(rows, (0,))

    def test_heartbeat_refreshes_session_before_close(self):
        class Commands:
            tick = 0

            def get(self, timeout):
                self.tick += 1
                if self.tick < 3:
                    raise queue.Empty
                return 'close'

        report(self, self.outbox, 'vision', 'vscode', Commands())
        self.assertEqual([name for name, _ in self.events], ['open', 'heartbeat', 'heartbeat', 'close'])
        versions = [state['version'] for _, state in self.events]
        self.assertEqual(versions, [1, 1, 1, 2])

    def test_personal_ide_close_replays_while_code_sync_is_waiting(self):
        devices = {name: {"token": name * 43, "personalSpace": "private", "projects": []}
                   for name in ("a", "b")}
        server = uvicorn.Server(uvicorn.Config(create_app(self.root / "personal.sqlite3", devices),
                                               log_level="error"))
        stop = threading.Event()
        running = threading.Event()
        advanced = threading.Event()

        def wait(predicate):
            deadline = time.monotonic() + 8
            while not predicate():
                if time.monotonic() >= deadline:
                    self.fail("timed out waiting for personal project occupancy")
                time.sleep(.025)

        def stalled_cycle(signal):
            running.set()
            signal.wait(10)
            advanced.set()

        with socket.socket() as endpoint:
            endpoint.bind(("127.0.0.1", 0))
            service = threading.Thread(target=server.run, kwargs={"sockets": [endpoint]}, daemon=True)
            service.start()
            try:
                wait(lambda: server.started)
                url = "http://127.0.0.1:" + str(endpoint.getsockname()[1])
                source_config = {"url": url, "device": "a", "projects": {}}
                holder = PersonalTaskLocks(ControlClient(source_config, devices["a"]["token"]))
                receiver = PersonalTaskLocks(ControlClient({**source_config, "device": "b"}, devices["b"]["token"]))
                project, ide, task = "owner/repo", str(uuid4()), str(uuid4())
                holder.event(project, ide, "vscode", 1, "open")
                with self.assertRaises(ControlDenied):
                    receiver.claim(task, project, lambda _: None)
                flow = PersonalSync(self.root, {}, source_config)
                with (patch.dict(os.environ, LANXI_DEVICE_TOKEN=devices["a"]["token"]),
                      patch.object(flow, "cycle", side_effect=stalled_cycle)):
                    workers = [threading.Thread(target=target, args=(stop,), daemon=True)
                               for target in (flow.worker, flow.watch_task_owners)]
                    for worker in workers:
                        worker.start()
                    try:
                        wait(running.is_set)
                        wait(lambda: flow.snapshot()["taskOwners"] is not None)
                        self.assertEqual(flow.snapshot()["taskOwners"]["projects"], {project: "a"})
                        enqueue_close(close_queue(self.root), holder, project, ide, "vscode", 2)
                        wait(lambda: flow.snapshot()["taskOwners"]["projects"] == {})
                        self.assertFalse(advanced.is_set())
                        self.assertTrue(receiver.claim(task, project, lambda _: None))
                    finally:
                        stop.set()
                        for worker in workers:
                            worker.join(5)
            finally:
                server.should_exit = True
                service.join(5)

    def test_two_real_reporter_processes_over_loopback(self):
        token = token_urlsafe(32)
        app = create_app(self.root / 'center.sqlite3', {self.device: {'token': token, 'projects': ['vision']}})
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        script = Path(__file__).resolve().parents[1] / 'ide_activity.py'
        processes = []

        def wait(check):
            deadline = time.monotonic() + 5
            while not check():
                if time.monotonic() >= deadline:
                    self.fail('timed out waiting for project activity')
                time.sleep(0.025)

        with socket.socket() as endpoint:
            endpoint.bind(('127.0.0.1', 0))
            worker = threading.Thread(target=lambda: server.run(sockets=[endpoint]), daemon=True)
            worker.start()
            config = self.root / 'local-config.json'
            device = dict(url=f'http://127.0.0.1:{endpoint.getsockname()[1]}', device=self.device,
                          projects={'vision': {'path': str(self.root)}})
            config.write_text(json.dumps({'coordination': device}), encoding='utf-8')
            client = ControlClient(device, token)

            def occupancy():
                return [entry['source'] for entry in client.request('vision', 'control')['humans']]

            try:
                wait(lambda: server.started)
                for source in ('vscode', 'pycharm'):
                    processes.append(subprocess.Popen(
                        [sys.executable, str(script), '--source', source, '--root', str(self.root),
                         '--config', str(config)], stdin=subprocess.PIPE,
                        env=dict(os.environ, LANXI_DEVICE_TOKEN=token)))
                wait(lambda: set(occupancy()) == {'vscode', 'pycharm'})
                processes[0].communicate(b'close\n', timeout=5)
                self.assertEqual(occupancy(), ['pycharm'])
                processes[1].communicate(b'', timeout=5)
                self.assertEqual(occupancy(), ['pycharm'])
                self.assertEqual([p.returncode for p in processes], [0, 0])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                server.should_exit = True
                worker.join(5)
