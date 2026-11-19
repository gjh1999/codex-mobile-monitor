"""Two independent repo roots and Codex homes, a filtered Git remote and a loopback model."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from automation import Approvals
from code_sync import value as git
from coordination_client import ControlClient
from handoff import HandoffError, find_session, native_publication_lock, session_info
from personal_memory import latest_session
from personal_sync import PersonalSync
from tests.smoke_handoff import Replies, native_turn
from universal_rpc import CodexRPC


def verify_desktop_resume(exe, machine, thread, directory):
    with subprocess.Popen(
        [exe, "app-server", "--stdio"],
        cwd=machine.root,
        env={**os.environ, "CODEX_HOME": str(machine.home)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    ) as process:
        relay_only = ControlClient({"url": "http://127.0.0.1", "device": "desktop", "projects": {}}, None)
        rpc = CodexRPC(Approvals(), custom_process=process, coordination=relay_only)
        try:
            rpc.personal_session = machine.threads.get
            result = rpc.call("thread/resume", {"threadId": thread, "excludeTurns": True})
            assert result["thread"]["id"] == thread
            assert Path(result["cwd"]).resolve() == directory.resolve()
            try:
                with native_publication_lock(machine.home, thread):
                    raise AssertionError("Live native thread allowed a second writer")
            except HandoffError:
                pass
        finally:
            rpc.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def verify(root):
    url = "https://github.com/fixture/personal-flow.git"
    bare = root / "remote.git"
    a = root / "laptop-project"
    a.mkdir()
    git(None, "config", "--global", "user.name", "Fixture")
    git(None, "config", "--global", "user.email", "fixture@example.invalid")
    git(None, "config", "--global", "url." + bare.as_uri() + ".insteadOf", url)
    git(None, "init", "--bare", "-b", "main", str(bare))
    git(bare, "config", "uploadpack.allowFilter", "true")
    git(a, "init", "-b", "main")
    (a / "source.py").write_text("base\n")
    git(a, "add", "source.py")
    git(a, "commit", "-m", "base")
    git(a, "remote", "add", "origin", url)
    git(a, "push", "-u", "origin", "main")
    share = root / "private-share"
    share.mkdir()

    def device(name, projects):
        parent = root / name
        parent.mkdir()
        return PersonalSync(
            parent,
            {"exchange": str(share), "checkoutRoot": str(parent / "repos"), "projects": projects},
            home=parent / "codex",
        )

    laptop = device("laptop", [{"path": str(a), "remote": url}])
    desktop = device("desktop", [])
    exe = shutil.which("codex.exe") or shutil.which("codex")
    stop = threading.Event()
    with ThreadingHTTPServer(("127.0.0.1", 0), Replies) as endpoint:
        threading.Thread(target=endpoint.serve_forever, daemon=True).start()
        try:
            tid = native_turn(exe, laptop.home, a, endpoint.server_port)
            laptop.cycle(stop)
            desktop.cycle(stop)
            b = Path(desktop.projects["fixture/personal-flow"])
            assert latest_session(b) == (tid, b), repr(desktop.status).encode("unicode_escape")
            # Both native clients need the same synthetic provider for this offline fixture.
            shutil.copyfile(laptop.home / "config.toml", desktop.home / "config.toml")
            verify_desktop_resume(exe, desktop, tid, b)
            (b / "source.py").write_text("edited on desktop\n")
            assert native_turn(exe, desktop.home, b, endpoint.server_port, tid) == tid
            desktop.cycle(stop)
            laptop.cycle(stop)
            assert latest_session(a) == (tid, a), laptop.status
            assert (a / "source.py").read_text() == "edited on desktop\n"
            assert native_turn(exe, laptop.home, a, endpoint.server_port, tid, "Return to laptop") == tid
            (transcript,) = find_session(laptop.home, tid)
            session_info(transcript.read_bytes())
            assert b"reply-on-return" in transcript.read_bytes()
            return {
                "devices": 2,
                "rounds": 3,
                "same_thread": True,
                "app_server_local_cwd": True,
                "live_writer_excluded": True,
                "code_head_equal": git(a, "rev-parse", "HEAD") == git(b, "rev-parse", "HEAD"),
                "relative_memory_path": ".codex/memories",
                "model": "loopback",
            }
        finally:
            endpoint.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        with patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(root / "gitconfig"), GIT_CONFIG_NOSYSTEM="1"):
            result = verify(root)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
