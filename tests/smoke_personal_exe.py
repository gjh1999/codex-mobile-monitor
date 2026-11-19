"""Verify the frozen desktop workers with two isolated runtimes and no user credentials."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from code_sync import value as git
from personal_relay import Records


def wait_for(test, processes):
    limit = time.monotonic() + 45
    while not test():
        if any(proc.poll() is not None for proc in processes):
            raise RuntimeError("A fixture desktop exited early")
        if time.monotonic() > limit:
            raise TimeoutError("Timed out waiting for the fixture desktops")
        time.sleep(0.2)


def verify(exe, root):
    remote = "https://github.com/fixture/frozen.git"
    bare = root / "remote.git"
    source = root / "work-laptop"
    source.mkdir()
    git(None, "config", "--global", "user.name", "Fixture")
    git(None, "config", "--global", "user.email", "fixture@example.invalid")
    git(None, "config", "--global", "url." + bare.as_uri() + ".insteadOf", remote)
    git(None, "init", "--bare", "-b", "main", str(bare))
    git(bare, "config", "uploadpack.allowFilter", "true")
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", remote)
    (source / "app.py").write_text("initial\n")
    git(source, "add", "app.py")
    git(source, "commit", "-m", "fixture base")
    git(source, "push", "-u", "origin", "main")
    share = root / "private"
    share.mkdir()
    prefix = Path("notes/Codex")
    (source / prefix).mkdir(parents=True)
    (source / prefix / "context.md").write_text("laptop note")
    target = root / "desktop" / "projects" / "fixture" / "frozen"
    windows = Path(os.environ["SystemRoot"])
    git_dir = Path(shutil.which("git")).parent
    environments = {}
    start = time.monotonic()

    @contextmanager
    def running(name):
        runtime = root / name
        startup = subprocess.STARTUPINFO(
            dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=subprocess.SW_HIDE
        )
        proc = subprocess.Popen(
            [str(runtime / exe.name)], cwd=runtime, env=environments[name], startupinfo=startup
        )
        try:
            yield [proc]
        finally:
            if proc.poll() is None:
                subprocess.run(
                    [str(windows / "System32/taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                proc.wait(timeout=10)

    db = Records(share / "lanxi-personal.sqlite", "personal")
    try:
        for name in ("laptop", "desktop"):
            runtime = root / name
            runtime.mkdir()
            binary = runtime / exe.name
            shutil.copy2(exe, binary)
            home = runtime / "codex"
            home.mkdir()
            if name == "laptop":
                (home / "AGENTS.md").write_text("personal fixture rules")
            settings = {
                "exchange": str(share),
                "intervalSeconds": 2,
                "checkoutRoot": str(runtime / "projects"),
                "projects": [{"remote": remote, "path": str(source), "memoryPath": prefix.as_posix()}]
                if name == "laptop"
                else [],
            }
            (runtime / "personal-sync.json").write_text(json.dumps(settings), encoding="utf-8")
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("CODEX", "DINGTALK", "CLAUDE", "LANXI", "GH_", "GITHUB_"))
            }
            env.update(
                CODEX_HOME=str(home),
                USERPROFILE=str(runtime),
                APPDATA=str(runtime),
                LOCALAPPDATA=str(runtime),
                QT_QPA_PLATFORM="offscreen",
                PATH=os.pathsep.join(map(str, (git_dir, windows / "System32", windows))),
            )
            environments[name] = env
        note = target / prefix / "context.md"
        key = "notes/fixture/frozen/context.md"
        with running("laptop") as processes:
            wait_for(lambda: db.read(key) == b"laptop note", processes)
        with running("desktop") as processes:
            wait_for(lambda: note.is_file() and note.read_text() == "laptop note", processes)
            assert (target / "app.py").read_text() == "initial\n"
            assert (root / "desktop/codex/AGENTS.md").read_text() == "personal fixture rules"
            (target / "app.py").write_text("saved on desktop\n")
            note.write_text("desktop note")
            wait_for(
                lambda: (
                    db.read(key) == b"desktop note" and git(bare, "show", "main:app.py") == "saved on desktop"
                ),
                processes,
            )
        with running("laptop") as processes:
            wait_for(
                lambda: (
                    (source / "app.py").read_text() == "saved on desktop\n"
                    and (source / prefix / "context.md").read_text() == "desktop note"
                ),
                processes,
            )
        assert git(source, "rev-parse", "HEAD") == git(target, "rev-parse", "HEAD")
        return {
            "frozen_devices": 2,
            "automatic_clone": True,
            "code_and_notes_returned": True,
            "relative_notes_path": prefix.as_posix(),
            "seconds": round(time.monotonic() - start, 2),
        }
    finally:
        # Windows may release mapped DLL and EXE files shortly after the process exits.
        time.sleep(0.3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lanxi-frozen-") as temp:
        work = Path(temp)
        with patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(work / "gitconfig"), GIT_CONFIG_NOSYSTEM="1"):
            result = verify(options.exe.resolve(), work)
    options.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
