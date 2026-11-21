"""Ordinary project enrollment using local Git and a simulated personal GitHub account."""

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

import plain_git as subject
from code_sync import LIMIT, SyncIssue, run, value


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

    def test_failed_login_preserves_ordinary_folder(self):
        self.token.side_effect = SyncIssue("请登录")
        with self.assertRaisesRegex(SyncIssue, "登录"):
            subject.promote(self.project)
        self.assertFalse((self.project / ".git").exists())
        self.api.assert_not_called()


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
        with patch.object(subject.subprocess, "run", return_value=Mock(returncode=1)) as git:
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
