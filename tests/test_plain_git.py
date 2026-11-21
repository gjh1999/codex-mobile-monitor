"""Ordinary project enrollment using local Git and a simulated personal GitHub account."""

import base64
import json
import os
import subprocess
import tempfile
import unittest
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

import plain_git as subject
from code_backups import saved_files
from code_sync import LIMIT, SyncIssue, commit_saved, run, value


class PlainGitTests(unittest.TestCase):
    def setUp(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(base / "global"),
                                                   "GIT_CONFIG_NOSYSTEM": "1"}))
        self.project = base / "vscodePronject" / "Satisfactry"
        self.project.mkdir(parents=True)
        self.bare = base / "github.git"
        run(None, "init", "--bare", str(self.bare))
        self.remote = None
        self.creations = 0
        self.token = self.enterContext(patch.object(subject, "_token", return_value="fixture-login"))
        self.api = self.enterContext(patch.object(subject, "_api", side_effect=self.api_request))
        self.push = self.enterContext(patch.object(subject, "_push", side_effect=self.git_push))

    def api_request(self, token, endpoint, data=None):
        self.assertEqual(token, "fixture-login")
        if endpoint == "/user":
            return {"login": "owner"}
        if endpoint == "/user/repos":
            self.assertIsNone(self.remote)
            self.assertTrue(data["private"])
            self.creations += 1
            self.remote = {"full_name": "owner/" + data["name"], "id": 432,
                           "private": True, "description": data["description"]}
            return self.remote
        self.assertEqual(endpoint, "/repos/owner/Satisfactry")
        return self.remote

    def git_push(self, folder, url, token, login):
        self.assertEqual(url, "https://github.com/owner/Satisfactry.git")
        run(folder, "push", str(self.bare), "HEAD:refs/heads/main")

    def test_first_commit_filters_and_retries_non_forced_push(self):
        files = {
            "source.py": "print(42)", ".gitignore": "generated.py\n!.lanxi/\n",
            ".gitattributes": "*.lfs.txt filter=lfs\n", "generated.py": "ignored",
            "large.lfs.txt": "fake lfs file", "raw.csv": "raw", ".lanxi/notes.txt": "metadata",
            ".codex/state.json": "metadata", "build/secret.cpp": "build",
            ".env.production": "credential", "credentials.json": "credential",
            "client_secret.json": "credential", ".ssh/private": "credential",
            "auth.json": "credential", "public.key": "credential", "binary.png": "\0binary",
        }
        for name, content in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode())
        with (self.project / "big.txt").open("wb") as stream:
            stream.truncate(LIMIT)
        child = self.project / "third-party"
        run(None, "init", str(child))
        (child / "nested.py").write_text("confidential")
        self.push.side_effect = SyncIssue("offline")
        with self.assertRaisesRegex(SyncIssue, "offline"):
            subject.promote(self.project)
        self.assertTrue(subject.managed(self.project))
        head = value(self.project, "rev-parse", "HEAD")
        self.push.side_effect = self.git_push
        self.assertEqual(subject.promote(self.project), "已接入 GitHub")
        self.assertEqual(value(self.bare, "rev-parse", "main"), head)
        tree = value(self.bare, "ls-tree", "--name-only", "-r", "main").splitlines()
        self.assertEqual(set(tree), {".gitattributes", ".gitignore", "source.py"})
        self.assertEqual(value(self.bare, "show", "main:source.py"), "print(42)")
        self.assertEqual(self.creations, 1)
        self.api.reset_mock()
        self.assertEqual(subject.promote(self.project), "已接入 GitHub")
        self.api.assert_not_called()
        # An edited origin must never be silently adopted on subsequent enrollments.
        run(self.project, "remote", "set-url", "--push", "origin", "https://example.invalid/other")
        with self.assertRaises(SyncIssue):
            subject.promote(self.project)
        self.assertEqual(self.push.call_count, 2)

    def test_conflicting_remote_keeps_local_commit_without_changing_remote(self):
        self.remote = {"id": 501, "full_name": "owner/Satisfactry", "description": "", "private": True}
        with self.assertRaisesRegex(SyncIssue, "同名"):
            subject.promote(self.project)
        self.assertTrue(value(self.project, "rev-parse", "HEAD"))
        self.assertEqual(value(self.project, "remote"), "")
        self.push.assert_not_called()
        self.assertEqual(self.creations, 0)

    def test_paired_sync_preserves_folder_sessions_across_failed_push_and_retry(self):
        import uvicorn
        from coordination_server import create_app
        from personal_memory import latest_session
        from personal_sync import PersonalSync
        from plain_projects import folder_id
        from project_library import set_project_root

        base = self.project.parents[1]
        source_library = base / "sender-library"
        relative = Path("vscodePronject", "Satisfactry")
        source_library.mkdir()
        self.project.parent.rename(source_library / relative.parent)
        self.project = source_library / relative
        receiver_library = base / "receiver-library"
        receiver_library.mkdir()
        (self.project / "source.py").write_bytes(b"version one\n")
        run(self.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        run(self.bare, "config", "uploadpack.allowFilter", "true")
        devices = {d: {"token": d * 32, "projects": [], "personalSpace": "fixture"} for d in ("a", "b")}
        server = uvicorn.Server(uvicorn.Config(create_app(base / "paired.db", devices), log_level="error"))
        endpoint = self.enterContext(socket.socket())
        endpoint.bind(("127.0.0.1", 0))
        url = "http://127.0.0.1:" + str(endpoint.getsockname()[1])
        thread = threading.Thread(target=lambda: server.run(sockets=[endpoint]), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(setattr, server, "should_exit", True)
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(server.started)
        workers = {}
        for d, library in (("a", source_library), ("b", receiver_library)):
            data = base / ("device-" + d)
            home = data / "codex"
            home.mkdir(parents=True)
            set_project_root(data, library)
            workers[d] = PersonalSync(data, {}, {"device": d, "url": url, "projects": {}}, home=home)
        tid = str(uuid.uuid4())
        name = "rollout-2026-11-19T01-02-03-" + tid + ".jsonl"
        session = workers["a"].home / "sessions/2026/11/19" / name
        session.parent.mkdir(parents=True)
        events = [("session_meta", {"id": tid, "cwd": str(self.project)}),
                  ("event_msg", {"type": "task_started", "turn_id": "1"}),
                  ("event_msg", {"type": "task_complete", "turn_id": "1"})]
        session.write_bytes(b"".join(json.dumps({"type": k, "payload": p}).encode() + b"\n" for k, p in events))

        def cycle(d):
            with patch.dict(os.environ, LANXI_DEVICE_TOKEN=d * 32):
                return workers[d].cycle(threading.Event())

        project = folder_id(relative.as_posix())
        self.push.side_effect = SyncIssue("simulated push failure")
        self.assertEqual(cycle("a")["folderGit"][project], "simulated push failure")
        self.assertEqual(cycle("a")["projects"], 0)
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(cycle("b")["projects"], 0)
        dest = receiver_library / relative
        self.assertEqual(latest_session(dest), (tid, dest))
        self.assertFalse((dest / ".git").exists())
        self.push.side_effect = self.git_push
        workers["a"].git_retries.clear()
        self.assertEqual(cycle("a")["folderGit"][project], "已接入 GitHub")
        self.assertEqual(self.push.call_count, 2)
        run(None, "config", "--global", "url." + self.bare.as_uri() + ".insteadOf",
            "https://github.com/owner/Satisfactry.git")
        cycle("a")
        result = cycle("b")
        self.assertEqual(result["projectPaths"], {"owner/satisfactry": str(dest)})
        self.assertEqual(result["status"]["owner/satisfactry"], "已同步")
        self.assertEqual(result["folderProjects"], {project: str(dest)})
        self.assertEqual((dest / "source.py").read_bytes(), b"version one\n")
        self.assertEqual(latest_session(dest), (tid, dest))
        self.assertEqual(workers["b"].snapshot()["sessions"][tid]["project"], project)
        self.assertEqual(self.creations, 1)

    def test_lost_create_response_recovers_own_remote_only(self):
        def drop_after_creation(token, endpoint, data=None):
            result = self.api_request(token, endpoint, data)
            if endpoint == "/user/repos":
                raise SyncIssue("lost reply")
            return result

        self.api.side_effect = drop_after_creation
        with self.assertRaisesRegex(SyncIssue, "lost reply"):
            subject.promote(self.project)
        self.api.side_effect = self.api_request
        self.assertEqual(subject.promote(self.project), "已接入 GitHub")
        self.assertEqual(self.creations, 1)

    def test_existing_or_nested_git_is_untouched(self):
        run(None, "init", str(self.project))
        child = self.project / "nested"
        child.mkdir()
        for folder in (self.project, child, self.bare):
            with self.subTest(folder=folder), self.assertRaises(SyncIssue):
                subject.promote(folder)
            self.assertFalse(subject.managed(folder))
        self.assertFalse((child / ".git").exists())
        self.token.assert_not_called()
        self.api.assert_not_called()

    def test_failed_login_keeps_local_git_and_retries_first_push(self):
        (self.project / "main.py").write_text("print(42)")
        self.token.side_effect = SyncIssue("请登录")
        with self.assertRaisesRegex(SyncIssue, "登录"):
            subject.promote(self.project)
        self.assertTrue(subject.managed(self.project))
        self.assertEqual(value(self.project, "show", "HEAD:main.py"), "print(42)")
        self.assertEqual(value(self.project, "log", "-1", "--format=%ae"), "noreply@lanxi.invalid")
        self.api.assert_not_called()
        self.token.side_effect = None
        self.assertEqual(subject.promote(self.project), "已接入 GitHub")
        self.assertEqual(value(self.project, "rev-parse", "HEAD"), value(self.bare, "rev-parse", "main"))

    def test_clone_subsequent_commit_and_server_backup_share_filters(self):
        (self.project / "main.py").write_text("print(42)")
        self.assertEqual(subject.promote(self.project), "已接入 GitHub")
        clone = self.bare.parent / "new-computer"
        run(None, "clone", "--branch", "main", str(self.bare), str(clone))
        run(clone, "config", "user.name", "qa")
        run(clone, "config", "user.email", "qa@localhost.invalid")
        for name in (".lanxi/notes.txt", ".codex/data.json", "dist/entry.js", "client_secret.json",
                     ".kube/config", "secret/further.txt", "credentials.txt", "build/output.py"):
            path = clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("sensitive")
        (clone / "main.py").write_text("print(43)")
        self.assertTrue(commit_saved(clone, ".codex/memories"))
        run(clone, "push", "origin", "main")
        self.assertEqual(value(self.bare, "ls-tree", "-r", "--name-only", "main"), "main.py")
        self.assertEqual(value(self.bare, "show", "main:main.py"), "print(43)")
        self.assertEqual([name for name, path, stamp in saved_files(clone, ".codex/memories")], ["main.py"])


class GithubTransportTests(unittest.TestCase):
    def test_gcm_authentication_is_specific_to_github(self):
        token = b"TEST_ONLY"
        with patch.object(subject.shutil, "which", return_value=None), patch.object(subject, "run") as git:
            git.return_value = subprocess.CompletedProcess([], 0, b"protocol=https\nhost=github.com\npassword=" + token)
            self.assertEqual(subject._token(), token.decode())
            self.assertEqual(git.call_args.kwargs["data"], b"protocol=https\nhost=github.com\n\n")
            git.return_value.stdout = b"protocol=https\nhost=elsewhere.example\npassword=" + token
            with self.assertRaises(SyncIssue):
                subject._token()

    def test_push_sends_credentials_only_in_child_environment(self):
        url, token = "https://github.com/test/repo.git", "test-only"
        with patch.object(subject.subprocess, "run", side_effect=[
            Mock(returncode=0, stdout=url.encode()), Mock(returncode=1)
        ]) as git:
            with self.assertRaises(SyncIssue) as failure:
                subject._push(Path.cwd(), url, token, "test")
        encoded = base64.b64encode(b"test:test-only").decode()
        args = git.call_args.args[0]
        self.assertNotIn(token, str(args) + str(failure.exception))
        self.assertNotIn(encoded, str(args) + str(failure.exception))
        self.assertIn(url, " ".join(args))
        self.assertIn("http.followRedirects=false", args)
        self.assertEqual(git.call_args.kwargs["env"]["LANXI_HTTP_AUTH"], "Authorization: Basic " + encoded)
        self.assertNotIn("--force", args)

    def test_api_rejects_redirect_without_forwarding_credentials(self):
        from urllib.request import Request

        req = Request("https://api.github.com/user", headers={"Authorization": "Bearer test-only"})
        self.assertIsNone(subject._NoRedirect().redirect_request(
            req, None, 302, "redirect", {}, "https://elsewhere.example/"))
        error = HTTPError(req.full_url, 302, "redirect", {}, None)
        with patch.object(subject, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(SyncIssue, "302"):
                subject._api("test-only", "/user")
