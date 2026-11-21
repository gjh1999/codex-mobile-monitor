import hashlib
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import uvicorn

from code_backups import CodeBackups
from code_sync import SyncIssue, run
from coordination_client import ControlClient
from coordination_server import create_app
from personal_sync import PersonalSync


class CodeBackupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = "owner/project"
        self.remote = f"https://github.com/{self.project}.git"
        self.repo = self.root / "project"
        run(None, "init", "-b", "main", str(self.repo))
        run(self.repo, "remote", "add", "origin", self.remote)
        run(self.repo, "config", "user.name", "Fixture")
        run(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "code.py").write_text("first\n", encoding="utf-8")
        (self.repo / "secrets.json").write_text("local only", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        (self.repo / "ignored.py").touch()
        run(self.repo, "add", "code.py", "secrets.json", ".gitignore")
        run(self.repo, "commit", "-m", "fixture")

        devices = {
            "desktop": {"token": "a" * 40, "personalSpace": "home", "projects": []},
            "laptop": {"token": "b" * 40, "personalSpace": "home", "projects": []},
            "other": {"token": "c" * 40, "personalSpace": "other", "projects": []},
        }
        app = create_app(self.root / "server.sqlite", devices, personal_only=True)
        listener = self.enterContext(socket.socket())
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(10)

        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(server.started)
        self.url = f"http://127.0.0.1:{port}"
        self.reader = CodeBackups(ControlClient({"url": self.url, "device": "laptop"}, "b" * 40))
        self.flow = PersonalSync(self.root / "desktop", {"intervalSeconds": 300, "memory": False, "projects": [
            {"remote": self.remote, "path": str(self.repo)},
        ]}, {"url": self.url, "device": "desktop"}, home=self.root / "codex")

    def test_automatic_and_manual_backup_survive_github_failure(self):
        stop = threading.Event()
        with (patch("personal_sync.device_token", return_value="a" * 40),
              patch("personal_sync.sync_project", side_effect=SyncIssue("GitHub 暂不可用")),
              patch("code_backup_server.RESERVE", 0)):
            automatic = self.flow.cycle(stop)
            self.assertEqual(automatic["status"][self.project], "GitHub 暂不可用")
            self.assertEqual(automatic["codeBackups"][self.project], "已备份")
            earlier = self.reader.list(self.project)[0]
            self.flow.cycle(stop)
            self.assertEqual(self.reader.list(self.project), [earlier])
            (self.repo / "code.py").write_text("changed without GitHub\n", encoding="utf-8")
            (self.repo / "new.py").write_text("new saved code\n", encoding="utf-8")
            request = self.flow.request_manual_sync()
            worker = threading.Thread(target=self.flow.worker, args=(stop,), daemon=True)
            worker.start()
            try:
                self.assertEqual(request.result(12)["codeBackups"][self.project], "已备份")
            finally:
                stop.set()
                worker.join(5)

        current = self.reader.list(self.project)[0]
        self.assertNotEqual(earlier["version"], current["version"])
        destination = self.root / "saved.zip"
        self.reader.download(self.project, "desktop", destination)
        with ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()), {".gitignore", "code.py", "new.py"})
            self.assertEqual(archive.read("code.py"), b"changed without GitHub\n")
        other = CodeBackups(ControlClient({"url": self.url, "device": "other"}, "c" * 40))
        self.assertEqual(other.list(self.project), [])
        with self.assertRaises(SyncIssue):
            other.download(self.project, "desktop", self.root / "other.zip")

        writer = CodeBackups(ControlClient({"url": self.url, "device": "desktop"}, "a" * 40))
        damaged = b"incomplete"
        with patch("code_backup_server.RESERVE", 0), self.assertRaisesRegex(SyncIssue, "格式"):
            writer.open(self.project, method="PUT", data=damaged, size=len(damaged), extra={
                "previous": current["version"], "sha256": hashlib.sha256(damaged).hexdigest(),
            })
        self.assertEqual(self.reader.list(self.project), [current])
