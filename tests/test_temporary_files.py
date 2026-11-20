import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from code_sync import run
from coordination_server import create_app
from personal_relay import EMPTY, Records
from temporary_files import FileExchange, TransferClient, TransferProblem


PROJECT = "fixture/project"
NAME = "model.dat"


class LocalClient(TransferClient):
    def __init__(self, api, device):
        self.api = api
        self.client = SimpleNamespace(device=device)

    def request(self, operation="", method="GET", body=None):
        headers = {"X-Lanxi-Device": self.client.device, "Authorization": "Bearer " + self.client.device * 43}
        payload = {"content": body} if isinstance(body, bytes) else {"json": body} if body is not None else {}
        reply = self.api.request(method, "/api/v1/temporary-files" + operation, headers=headers, **payload)
        if reply.status_code != 200:
            raise TransferProblem(str(reply.json()))
        return reply.json() if reply.headers["content-type"].startswith("application/json") else reply.content


class FileExchangeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = root = Path(temp.name)
        config = {device: {"token": device * 43, "personalSpace": space, "projects": []}
                  for device, space in (("a", "owner"), ("b", "owner"), ("c", "unrelated"))}
        db = root / "server.sqlite3"
        self.original = b"original source bytes" * 64
        self.blobs = root / "temporary-cloud"
        relay = Records(db, "owner")
        relay.put(f"large-inventory/{PROJECT}/a", EMPTY,
                  json.dumps({"files": {NAME: len(self.original)}, "name": "desktop"}).encode())
        self.api = TestClient(create_app(db, config, personal_only=True, temporary_dir=self.blobs))
        self.addCleanup(self.api.close)
        self.stop = threading.Event()
        self.clients = [LocalClient(self.api, device) for device in ("a", "b", "c")]
        self.exchanges = []
        self.paths = []
        for device in ("a", "b"):
            directory = root / device
            run(None, "init", str(directory))
            flow = SimpleNamespace(snapshot=lambda folder=directory: {"projects": {PROJECT: str(folder)}})
            self.exchanges.append(FileExchange(directory, flow))
            self.paths.append(directory / NAME)
        self.paths[0].write_bytes(self.original)
        run(self.paths[0].parent, "add", NAME)
        self.key = self.clients[1].request("", "POST", {"project": PROJECT, "source": "a", "path": NAME})["key"]
        for name, value in (("temporary_files.LIMIT", 64), ("temporary_files.CHUNK", 128),
                            ("temporary_file_server.CHUNK", 128), ("temporary_file_server.RESERVE", 0)):
            fixture = patch(name, value)
            fixture.start()
            self.addCleanup(fixture.stop)

    def process(self, machine):
        client = self.clients[machine]
        row = next(row for row in client.request() if row["key"] == self.key)
        self.exchanges[machine].operation(client, row, self.stop)

    def state(self):
        return self.clients[0].request()[0]["state"]

    def test_resume_return_and_remove_temporary_bytes_after_confirmed_application(self):
        source, remote = self.paths
        old = self.original
        info = {"sha256": hashlib.sha256(old).hexdigest(), "size": len(old)}
        with self.assertRaises(TransferProblem):
            self.clients[1].request(f"/{self.key}/original", "POST", info)
        self.clients[0].request(f"/{self.key}/original", "POST", info)
        self.clients[0].request(f"/{self.key}/original?offset=0", "PUT", old[:128])
        self.process(0)
        self.assertEqual(self.clients[2].request(), [])
        self.process(1)
        self.assertEqual(remote.read_bytes(), old)
        edited = b"edited laptop bytes" * 95
        remote.write_bytes(edited)
        self.exchanges[1].submit(self.key)
        self.process(1)
        self.assertEqual(self.state(), "returned")
        self.assertEqual(source.read_bytes(), old)
        self.process(0)
        self.assertEqual(source.read_bytes(), edited)
        self.assertEqual(self.state(), "completed")
        self.assertFalse((self.blobs / self.key).exists())
        self.assertEqual(self.clients[0].request()[0]["original"]["sha256"], info["sha256"])

    def test_source_edit_conflict_keeps_both_local_versions(self):
        self.process(0)
        self.process(1)
        source, remote = self.paths
        source.write_bytes(b"source was edited")
        remote.write_bytes(b"laptop independent edit")
        self.exchanges[1].submit(self.key)
        self.process(1)
        self.process(0)
        self.assertEqual(self.state(), "conflict")
        self.assertEqual(source.read_bytes(), b"source was edited")
        self.assertEqual(remote.read_bytes(), b"laptop independent edit")
        self.assertFalse((self.blobs / self.key).exists())
