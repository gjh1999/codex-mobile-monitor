import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn

from coordination_client import ControlClient
from coordination_server import create_app
from personal_relay import EMPTY, Records, digest
from server_diagnostics import CheckFailed, record_roundtrip, server_checks


class ServerDiagnosticsTests(unittest.TestCase):
    def test_reports_real_endpoints_and_keeps_user_records(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        database = root / "coordinator.sqlite"
        releases = root / "releases"
        releases.mkdir()
        (releases / "latest.json").write_text(json.dumps({"version": "37", "size": 1, "sha256": "f" * 64}))
        (releases / "Lanxi-R37-Windows-Private.scope").write_text(digest(b"space"))
        token = "a" * 43
        app = create_app(database, {"source": {"personalSpace": "space", "token": token, "projects": []}},
                         personal_only=True, release_dir=releases)
        listener = self.enterContext(socket.socket())
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 10)
        self.addCleanup(setattr, server, "should_exit", True)
        deadline = time.monotonic() + 10
        while not server.started:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)

        client = ControlClient({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "device": "source",
                                "projects": {}}, token)
        store = Records(database, "space")
        sensitive = "do-not-print-user-content"
        seeds = {"repo/test/project": b"{}", "sessions/test/project/file": sensitive.encode(),
                 "codex-live/source": json.dumps({"v": 1, "approvals": [{"details": sensitive}],
                                                 "decisions": [], "receipts": []}).encode()}
        for key, data in seeds.items():
            store.put(key, EMPTY, data)
        def run():
            rows = {}
            server_checks(client, lambda name, status, detail: rows.update({name: (status, detail)}), "test/project")
            self.assertNotIn(sensitive, str(rows))
            return rows

        rows = run()
        self.assertTrue(all(status == "passed" for status, _ in rows.values()), rows)
        self.assertIn("1 条", rows["已完成会话索引"][1])
        (releases / "latest.json").unlink()
        rows = run()
        self.assertEqual(rows["私人更新清单"][0], "failed")
        self.assertEqual(rows["服务器认证读写"][0], "passed")
        actual = store.index()
        self.assertEqual({key: rev for key, rev in actual.items() if rev != EMPTY},
                         {key: digest(data) for key, data in seeds.items()})
        self.assertEqual(len(actual), len(seeds) + 1)

    def test_cleanup_after_mismatched_reply(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Records(root / "test.sqlite", "space")
        store.client = SimpleNamespace(device="fixture")
        with patch.object(store, "read", return_value=b"wrong"), self.assertRaises(CheckFailed):
            record_roundtrip(store)
        self.assertEqual(list(store.index().values()), [EMPTY])
