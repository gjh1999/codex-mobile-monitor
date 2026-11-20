import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from code_sync import SyncIssue, run, sync_project
from coordination_server import create_app
from personal_memory import replace
from personal_sync import PersonalSync
from project_library import set_project_root


def git(repo, *args):
    return run(repo, *args).stdout.decode().strip()


class FlowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        self.url = "https://github.com/example/flow.git"
        bare = root / "remote.git"
        config = root / "gitconfig"
        env = patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM="1")
        env.start()
        self.addCleanup(env.stop)
        git(None, "config", "--global", "user.name", "Fixture")
        git(None, "config", "--global", "user.email", "flow@example.invalid")
        git(None, "init", "--bare", "-b", "main", str(bare))
        git(bare, "config", "uploadpack.allowFilter", "true")
        git(None, "config", "--global", "url." + bare.as_uri() + ".insteadOf", self.url)
        self.a = root / "notebook" / "project A"
        self.b = root / "desktop" / "different name"
        self.a.mkdir(parents=True)
        git(self.a, "init", "-b", "main")
        git(self.a, "remote", "add", "origin", self.url)
        (self.a / "a.py").write_text("first\n")
        git(self.a, "add", "a.py")
        git(self.a, "commit", "-m", "base")
        git(self.a, "push", "-u", "origin", "main")
        self.share = root / "share"
        self.share.mkdir()

    def test_projects_follow_relative_folder_on_new_device(self):
        relative = Path("客户甲/研发/项目 A")
        original = self.a
        self.a = self.root / "source-library" / relative
        self.a.parent.mkdir(parents=True)
        original.rename(self.a)
        workers = []
        for machine in ("source", "receiver"):
            data = self.root / (machine + "-data")
            data.mkdir()
            parent = self.root / (machine + "-library")
            parent.mkdir(exist_ok=True)
            set_project_root(data, parent)
            workers.append(PersonalSync(data, {"exchange": str(self.share)}, home=data / "codex"))
        stop = threading.Event()
        sender, receiver = workers
        sender.cycle(stop)
        receiver.cycle(stop)
        clone = self.root / "receiver-library" / relative
        self.assertEqual((clone / "a.py").read_text(), "first\n")
        catalog = json.loads(sender.store().read("repo/example/flow"))
        self.assertEqual(catalog["relativePath"], relative.as_posix())
        self.assertNotIn(str(self.root), json.dumps(catalog))
        (clone / "a.py").write_text("from the other device\n")
        receiver.cycle(stop)
        sender.cycle(stop)
        self.assertEqual((self.a / "a.py").read_text(), "from the other device\n")

    def test_bidirectional_filtered_clone_and_conflicts(self):
        data = os.urandom(32000)
        (self.a / "weights.bin").write_bytes(data)
        git(self.a, "add", "weights.bin")
        git(self.a, "commit", "-m", "fixture data")
        git(self.a, "push")
        blob = git(self.a, "rev-parse", "HEAD:weights.bin")
        with patch("code_sync.LIMIT", 16384):
            sync_project(self.b, self.url)
            self.assertFalse((self.b / "weights.bin").exists())
            self.assertNotEqual(run(self.b, "cat-file", "-e", blob, check=False).returncode, 0)
            self.assertEqual((self.b / "a.py").read_text(), "first\n")
            (self.a / "a.py").write_text("saved\n")
            (self.a / "huge.py").write_bytes(data)
            sync_project(self.a, self.url)
            sync_project(self.b, self.url)
            self.assertEqual((self.b / "a.py").read_text(), "saved\n")
            self.assertFalse((self.b / "huge.py").exists())
            (self.b / "new.py").write_text("from B\n")
            sync_project(self.b, self.url)
            sync_project(self.a, self.url)
            self.assertEqual((self.a / "new.py").read_text(), "from B\n")
            (self.b / "a.py").write_text("conflicting B\n")
            (self.a / "a.py").write_text("conflicting A\n")
            sync_project(self.a, self.url)
            with self.assertRaisesRegex(SyncIssue, "冲突"):
                sync_project(self.b, self.url)
            self.assertEqual((self.b / "a.py").read_text(), "conflicting B\n")

    def test_existing_clone_recovers_newly_allowed_blobs_at_same_commit(self):
        medium = self.a / "medium.dat"
        oversized = self.a / "oversized.dat"
        medium.write_bytes(os.urandom(20_000))
        oversized.write_bytes(os.urandom(40_000))
        git(self.a, "add", "medium.dat", "oversized.dat")
        git(self.a, "commit", "-m", "test threshold migration")
        git(self.a, "push")
        with patch("code_sync.LIMIT", 16_384):
            sync_project(self.b, self.url)
        self.assertFalse((self.b / medium.name).exists())
        git(self.b, "config", "--unset", "lanxi.blobLimit")
        with patch("code_sync.LIMIT", 32_768):
            sync_project(self.b, self.url)
        self.assertEqual((self.b / medium.name).read_bytes(), medium.read_bytes())
        self.assertFalse((self.b / oversized.name).exists())
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), git(self.b, "rev-parse", "HEAD"))

    def test_sparse_size_transitions_and_lfs(self):
        self.b.mkdir(parents=True)
        git(self.a, "config", "filter.lfs.process", "git-lfs filter-process")
        (self.a / ".gitattributes").write_text("*.weights filter=lfs diff=lfs merge=lfs -text\n")
        (self.a / "data.weights").write_bytes(os.urandom(33000))
        (self.a / "small.bin").write_bytes(b"small")
        (self.a / "big.bin").write_bytes(os.urandom(32000))
        git(self.a, "add", ".")
        git(self.a, "commit", "-m", "sizes and LFS")
        git(self.a, "push")
        first_blob = git(self.a, "rev-parse", "HEAD:big.bin")
        with patch("code_sync.LIMIT", 16384):
            sync_project(self.b, self.url)
            self.assertTrue((self.b / "data.weights").read_bytes().startswith(b"version https://git-lfs."))
            self.assertFalse(any(p.is_file() for p in (self.b / ".git/lfs/objects").rglob("*")))
            (self.a / "big.bin").write_bytes(b"now small")
            (self.a / "small.bin").write_bytes(os.urandom(34000))
            git(self.a, "add", ".")
            git(self.a, "commit", "-m", "reverse size")
            git(self.a, "push")
            second_blob = git(self.a, "rev-parse", "HEAD:small.bin")
            sync_project(self.b, self.url)
            self.assertEqual((self.b / "big.bin").read_bytes(), b"now small")
            self.assertFalse((self.b / "small.bin").exists())
            for oid in (first_blob, second_blob):
                self.assertNotEqual(run(self.b, "cat-file", "-e", oid, check=False).returncode, 0)

    def test_overlap_free_changes_and_protection_of_staging(self):
        sync_project(self.b, self.url)
        (self.b / "new.unknown").write_text("custom source\n")
        (self.a / "a.py").write_text("edited locally\n")
        sync_project(self.a, self.url)
        sync_project(self.b, self.url)
        sync_project(self.a, self.url)
        self.assertEqual((self.b / "a.py").read_text(), "edited locally\n")
        self.assertEqual((self.a / "new.unknown").read_text(), "custom source\n")
        (self.a / "a.py").write_text("stage me")
        git(self.a, "add", "a.py")
        staged = git(self.a, "write-tree")
        with self.assertRaisesRegex(SyncIssue, "暂存"):
            sync_project(self.a, self.url)
        self.assertEqual(staged, git(self.a, "write-tree"))

    def test_missing_filter_leaves_destination_uncreated(self):
        git(self.root / "remote.git", "config", "uploadpack.allowFilter", "false")
        with self.assertRaisesRegex(SyncIssue, "过滤"):
            sync_project(self.b, self.url)
        self.assertFalse(self.b.exists())

    def test_memory_replacement_recovers_after_disk_error(self):
        path = self.root / "note.md"
        path.write_bytes(b"old")
        for target in ("pathlib.Path.write_bytes", "os.link"):
            with patch(target, side_effect=OSError("disk busy")), self.assertRaises(OSError):
                replace(path, b"old", b"new", self.root / "backups")
            self.assertEqual(path.read_bytes(), b"old")

    def test_catalog_notes_global_rules_resumable_history_and_long_term(self):
        stop = threading.Event()

        def machine(name, reg):
            folder = self.root / name
            home = folder / "codex"
            home.mkdir(parents=True)
            with closing(sqlite3.connect(home / "memories_1.sqlite")) as db, db:
                db.execute(
                    "CREATE TABLE stage1_outputs(thread_id TEXT PRIMARY KEY, source_updated_at INTEGER, raw_memory TEXT, rollout_summary TEXT, rollout_slug TEXT, generated_at INTEGER)"
                )
            return PersonalSync(
                folder / "runtime",
                {"exchange": str(self.share), "checkoutRoot": str(folder / "clones"), "projects": reg},
                home=home,
            )

        a = machine("alpha", [{"remote": self.url, "path": str(self.a), "memoryPath": "notes/Codex"}])
        b = machine("beta", [])
        tid = str(uuid.uuid4())
        turn = str(uuid.uuid4())
        folder = a.home / "sessions/2026/11/19"
        folder.mkdir(parents=True)
        name = f"rollout-2026-11-19T01-02-03-{tid}.jsonl"

        def event(kind, payload):
            return json.dumps({"type": kind, "payload": payload}).encode() + b"\n"

        original = (
            event("session_meta", {"id": tid, "cwd": str(self.a)})
            + event("event_msg", {"type": "task_started", "turn_id": turn})
            + event("event_msg", {"type": "task_complete", "turn_id": turn})
        )
        (folder / name).write_bytes(original)
        mem = self.a / "notes/Codex"
        mem.mkdir(parents=True)
        (mem / "decision.md").write_text("A note")
        (a.home / "AGENTS.md").write_text("global rule")
        with closing(sqlite3.connect(a.home / "memories_1.sqlite")) as db, db:
            db.execute(
                "INSERT INTO stage1_outputs VALUES(?,1,?,?,NULL,1)", (tid, "fixture memory", "summary")
            )
        a.cycle(stop)
        b.cycle(stop)
        view = b.snapshot()
        self.assertEqual(view["status"], {"example/flow": "已同步"})
        dest = Path(view["projects"]["example/flow"])
        self.assertEqual((dest / "notes/Codex/decision.md").read_text(), "A note")
        self.assertEqual((b.home / "AGENTS.md").read_text(), "global rule")
        self.assertEqual((b.home / "sessions/2026/11/19" / name).read_bytes(), original)
        self.assertEqual(view["sessions"][tid]["cwd"], str(dest))
        with closing(sqlite3.connect(b.home / "memories_1.sqlite")) as db:
            self.assertEqual(
                db.execute("SELECT raw_memory FROM stage1_outputs").fetchone(), ("fixture memory",)
            )
        self.assertFalse(git(self.a, "ls-files", "notes"))
        (dest / "notes/Codex/decision.md").write_text("B update")
        second = (
            original
            + event("event_msg", {"type": "task_started", "turn_id": "next"})
            + event("event_msg", {"type": "task_complete", "turn_id": "next"})
        )
        (b.home / "sessions/2026/11/19" / name).write_bytes(second)
        b.cycle(stop)
        a.cycle(stop)
        self.assertEqual((folder / name).read_bytes(), second)
        self.assertEqual((mem / "decision.md").read_text(), "B update")
        (a.home / "AGENTS.md").write_text("local")
        (b.home / "AGENTS.md").write_text("other")
        a.cycle(stop)
        b.cycle(stop)
        self.assertEqual((b.home / "AGENTS.md").read_text(), "other")
        self.assertIn("记忆", b.snapshot()["status"])
        a.settings["projects"][0]["memoryPath"] = "context/Codex"
        a.cycle(stop)
        b.cycle(stop)
        self.assertEqual((self.a / "context/Codex/decision.md").read_text(), "B update")
        self.assertEqual((dest / "context/Codex/decision.md").read_text(), "B update")
        (self.a / "context/Codex/decision.md").unlink()
        a.cycle(stop)
        b.cycle(stop)
        self.assertFalse((dest / "context/Codex/decision.md").exists())
        with closing(sqlite3.connect(b.home / "memories_1.sqlite")) as db, db:
            db.execute("DELETE FROM stage1_outputs WHERE thread_id=?", (tid,))
        b.cycle(stop)
        a.cycle(stop)
        with closing(sqlite3.connect(a.home / "memories_1.sqlite")) as db:
            self.assertIsNone(
                db.execute("SELECT raw_memory FROM stage1_outputs WHERE thread_id=?", (tid,)).fetchone()
            )

    def test_private_lanxi_channel_scoped_and_conditional(self):
        devices = {
            name: {"token": name * 32, "projects": [], "personalSpace": space}
            for name, space in [("a", "alice"), ("b", "alice"), ("c", "bob")]
        }
        devices["d"] = {"token": "d" * 32, "projects": []}
        app = TestClient(create_app(self.root / "center.db", devices))

        def headers(d):
            return {"Authorization": "Bearer " + d * 32, "X-Lanxi-Device": d}

        path = "/api/v1/personal/record?key=global%2FAGENTS.md"
        self.assertEqual(
            app.put(path + "&previous=-", headers=headers("a"), content=b"hello").status_code, 204
        )
        self.assertEqual(app.get(path, headers=headers("b")).content, b"hello")
        self.assertEqual(
            app.put(path + "&previous=-", headers=headers("b"), content=b"other").status_code, 409
        )
        self.assertEqual(app.get(path, headers=headers("c")).status_code, 404)
        self.assertEqual(app.get(path, headers=headers("d")).status_code, 403)

    def test_new_device_discovers_project_over_lanxi_http(self):
        import uvicorn

        devices = {d: {"token": d * 32, "projects": [], "personalSpace": "one"} for d in ("a", "b")}
        server = uvicorn.Server(uvicorn.Config(create_app(self.root / "api.db", devices), log_level="error"))
        stop = threading.Event()
        with socket.socket() as endpoint:
            endpoint.bind(("127.0.0.1", 0))
            url = "http://127.0.0.1:" + str(endpoint.getsockname()[1])
            worker = threading.Thread(target=lambda: server.run(sockets=[endpoint]), daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(server.started)
                for d, projects in [("a", [{"remote": self.url, "path": str(self.a)}]), ("b", [])]:
                    parent = self.root / ("device-" + d)
                    home = parent / "codex"
                    home.mkdir(parents=True)
                    if d == "a":
                        (home / "AGENTS.md").write_text("my global rules")
                        notes = self.a / ".codex/memories"
                        notes.mkdir(parents=True)
                        (notes / "decision.md").write_text("fixture decision")
                    sync = PersonalSync(
                        parent,
                        {"checkoutRoot": str(parent / "work"), "projects": projects},
                        {"url": url, "device": d, "projects": {}},
                        home=home,
                    )
                    with patch.dict(os.environ, LANXI_DEVICE_TOKEN=d * 32):
                        sync.cycle(stop)
                    self.assertEqual(sync.status, {"example/flow": "已同步"})
                    project = Path(sync.projects["example/flow"])
                    self.assertEqual(
                        (project / ".codex/memories/decision.md").read_text(), "fixture decision"
                    )
                    self.assertEqual((home / "AGENTS.md").read_text(), "my global rules")
            finally:
                server.should_exit = True
                worker.join(5)
