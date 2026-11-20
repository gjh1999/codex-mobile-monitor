"""Run the packaged desktop against disposable repositories on one or two Windows PCs."""

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from code_sync import run
from personal_relay import Records

REMOTE = "https://github.com/lanxi-check/fixture.git"
PROJECT = "lanxi-check/fixture"
MEMORY = Path("notes/Codex")
BIG_FILE = "fixture-large.bin"
SHARE_PREFIX = "Lanxi-R21-test-"
TIMEOUT = 90


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    write(temp, json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(path)


def git(repo, *args):
    result = run(repo, *args, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Git {args[0]} 失败\n{detail[-1800:]}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(label, condition, proc, seconds=TIMEOUT):
    deadline = time.monotonic() + seconds
    while True:
        if proc.poll() is not None:
            if proc.startup_error.is_file():
                raise RuntimeError(proc.startup_error.read_text(encoding="utf-8"))
            raise RuntimeError(
                f"{label}时澜汐提前退出，退出码 {proc.returncode}。请确认其他澜汐已从托盘退出。"
            )
        try:
            if condition():
                return
        except (FileNotFoundError, PermissionError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{label}超时（{seconds} 秒）")
        time.sleep(0.25)


def read_is(path, expected):
    return path.read_text(encoding="utf-8").strip() == expected


def require_share(value):
    root = Path(value.strip().strip('"'))
    check(root.is_absolute() and root.is_dir(), "请输入两台电脑都能读写的已存在共享目录绝对路径。")
    check(
        not any((p / ".git").exists() for p in (root, *root.parents)), "共享测试目录需要位于 Git 项目之外。"
    )
    return root


def source_fixture(exchange, repo, profile):
    bare = exchange / "remote.git"
    git(None, "init", "--bare", "-b", "main", str(bare))
    git(bare, "config", "uploadpack.allowFilter", "true")
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "remote", "add", "origin", REMOTE)
    write(repo / "app.py", "# source change\n")
    with (repo / BIG_FILE).open("wb") as stream:
        stream.truncate(12 * 1024 * 1024)
    git(repo, "add", "app.py", BIG_FILE)
    git(repo, "commit", "-m", "Lanxi fixture base")
    git(repo, "push", "-u", "origin", "main")
    write(repo / MEMORY / "decision.md", "source note\n")
    write(profile / "AGENTS.md", "fixture global rules\n")
    thread = str(uuid.uuid4())
    turn = str(uuid.uuid4())
    relative = "sessions/2026/11/19/rollout-2026-11-19T01-02-03-" + thread + ".jsonl"
    events = (
        {"type": "session_meta", "payload": {"id": thread, "cwd": str(repo)}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
    )
    write(profile / relative, "".join(json.dumps(event) + "\n" for event in events))
    return {
        "schema": 1,
        "id": thread,
        "blob": git(repo, "rev-parse", "HEAD:" + BIG_FILE),
        "session_hash": hash_file(profile / relative),
    }


def session_path(fixture):
    return Path("sessions/2026/11/19") / f"rollout-2026-11-19T01-02-03-{fixture['id']}.jsonl"


def hash_file(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@contextmanager
def isolated_git(root, exchange):
    bare = exchange / "remote.git"
    uri = bare.as_uri() if not bare.drive.startswith("\\\\") else "file:////" + bare.as_uri()[7:]
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    inherited.update(GIT_CONFIG_GLOBAL=str(root / "test.gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    with patch.dict(os.environ, inherited, clear=True):
        git(None, "config", "--global", "user.name", "Lanxi verification")
        git(None, "config", "--global", "user.email", "fixture@example.invalid")
        git(None, "config", "--global", f"url.{uri}.insteadOf", REMOTE)
        git(None, "config", "--global", "safe.directory", bare.as_posix())
        yield


def prepare_runtime(exe, runtime, exchange, source=None):
    runtime.mkdir()
    binary = runtime / exe.name
    shutil.copy2(exe, binary)
    profile = runtime / "codex"
    profile.mkdir()
    projects = [{"remote": REMOTE, "path": str(source), "memoryPath": MEMORY.as_posix()}] if source else []
    write_json(
        runtime / "personal-sync.json",
        {
            "exchange": str(exchange),
            "intervalSeconds": 2,
            "checkoutRoot": str(runtime / "projects"),
            "projects": projects,
        },
    )
    return profile


@contextmanager
def running(exe, runtime, *, local_appdata=None, arguments=()):
    windows = Path(os.environ["SystemRoot"])
    git_dir = Path(shutil.which("git")).parent
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODEX", "DINGTALK", "CLAUDE", "LANXI", "GH_", "GITHUB_", "OPENAI"))
    }
    probe = uuid.uuid4().hex
    write(runtime / ".lanxi-verification", probe)
    env.update(
        CODEX_HOME=str(runtime / "codex"),
        USERPROFILE=str(runtime),
        APPDATA=str(runtime),
        LOCALAPPDATA=str(local_appdata or runtime.with_name(runtime.name + "-user-data")),
        QT_QPA_PLATFORM="offscreen",
        LANXI_VERIFICATION_INSTANCE=probe,
        PYINSTALLER_RESET_ENVIRONMENT="1",
        PATH=os.pathsep.join(map(str, (git_dir, windows / "System32", windows))),
    )
    startup = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=subprocess.SW_HIDE)
    kernel = ctypes.windll.kernel32
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    job = kernel.CreateJobObjectW(None, None)
    try:
        proc = subprocess.Popen([str(runtime / exe.name), *arguments], env=env, cwd=runtime, startupinfo=startup)
        proc.startup_error = runtime / ".lanxi-verification-error"
        assigned = job and kernel.AssignProcessToJobObject(job, int(proc._handle))
        try:
            yield proc
        finally:
            if not (assigned and kernel.TerminateJobObject(job, 0)) and proc.poll() is None:
                result = subprocess.run(
                    [str(windows / "System32/taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=15,
                )
                if result.returncode and proc.poll() is None:
                    raise RuntimeError(f"无法关闭本次验收的临时澜汐进程（PID {proc.pid}）。")
            proc.wait(timeout=10)
            time.sleep(0.35)
    finally:
        if job:
            kernel.CloseHandle(job)


def verify(exe, root, exchange, mode, report):
    source, receiver = root / "source-work", root / "receiver"
    source_runtime = root / "source"
    note_key = f"notes/{PROJECT}/decision.md"
    ready = exchange / "source-ready.json"
    checked = exchange / "source-verified.json"
    returned = exchange / "receiver-returned.json"
    if mode == "receiver":
        check(
            exchange.name.startswith(SHARE_PREFIX) and ready.is_file(),
            f"找不到源机验收标记，请输入源机显示的完整测试子目录。\n{exchange}",
        )
    database = Records(exchange / "lanxi-personal.sqlite", "personal")
    with isolated_git(root, exchange):
        if mode != "receiver":
            source_profile = prepare_runtime(exe, source_runtime, exchange, source)
            fixture = source_fixture(exchange, source, source_profile)
            with running(exe, source_runtime) as proc:
                session_key = f"sessions/{PROJECT}/{session_path(fixture).name}"
                wait_for(
                    "源机发布项目和记忆",
                    lambda: (
                        database.read(note_key) == b"source note\n" and database.read(session_key) is not None
                    ),
                    proc,
                )
                report("源机启动并发布项目记忆")
                if mode == "source":
                    write_json(ready, fixture)
                    print(f"\n在另一台电脑选 3，输入此目录\n{exchange}\n", flush=True)
                    verify_return(source, returned, fixture, proc, 20 * 60)
                    write_json(checked, {"id": fixture["id"], "status": "passed"})
                    report("源机收到接收机保存的代码和同路径项目记忆")
                    return
        else:
            fixture = json.loads(ready.read_text(encoding="utf-8"))
            check(
                fixture.get("schema") == 1
                and str(uuid.UUID(fixture["id"])) == fixture["id"]
                and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", fixture["blob"])
                and re.fullmatch(r"[a-f0-9]{64}", fixture["session_hash"]),
                "源机的验收标记格式无效。",
            )
            check(not returned.exists(), "这组验收已回传。请在源机开启一组新验收。")

        profile = prepare_runtime(exe, receiver, exchange)
        target = receiver / "projects" / Path(PROJECT)
        note = target / MEMORY / "decision.md"
        with running(exe, receiver) as proc:
            wait_for("接收机自动克隆并收到同路径项目记忆", lambda: read_is(note, "source note"), proc)
            check(read_is(target / "app.py", "# source change"), "接收端克隆的代码内容无效。")
            report("从空工作区自动克隆，项目记忆落在 notes/Codex/decision.md")
            wait_for(
                "复制全局 AGENTS.md 和会话记录",
                lambda: (
                    read_is(profile / "AGENTS.md", "fixture global rules")
                    and hash_file(profile / session_path(fixture)) == fixture["session_hash"]
                ),
                proc,
            )
            report("全局 AGENTS.md 与已完成会话记录一致")
            large_object = run(target, "cat-file", "-e", fixture["blob"], check=False)
            check(
                not (target / BIG_FILE).exists() and large_object.returncode != 0,
                "12 MiB 测试文件进入了接收端工作区或 Git 对象库。",
            )
            report("12 MiB 大文件未进入工作区或 Git 对象库")
            write(target / "app.py", "# receiver change\n")
            write(note, "receiver note\n")
            bare = exchange / "remote.git"
            wait_for(
                "接收机发布修改",
                lambda: (
                    database.read(note_key) == b"receiver note\n"
                    and git(bare, "show", "main:app.py") == "# receiver change"
                ),
                proc,
            )
            write_json(returned, {"id": fixture["id"], "head": git(target, "rev-parse", "HEAD")})
            report("接收机自动提交和回传代码、项目记忆")
            if mode == "receiver":
                wait_for(
                    "源机确认收到回传",
                    lambda: (
                        checked.is_file()
                        and json.loads(checked.read_text(encoding="utf-8"))["id"] == fixture["id"]
                    ),
                    proc,
                )
                report("源机确认回传成功")
                return
        with running(exe, source_runtime) as proc:
            verify_return(source, returned, fixture, proc, TIMEOUT)
            report("源端收到回传，双方 Git 提交一致")


def verify_return(source, marker, fixture, proc, seconds):
    def returned():
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        return (
            receipt["id"] == fixture["id"]
            and read_is(source / "app.py", "# receiver change")
            and read_is(source / MEMORY / "decision.md", "receiver note")
            and receipt["head"] == git(source, "rev-parse", "HEAD")
        )

    wait_for("源机收到回传的代码和记忆", returned, proc, seconds)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("local", "source", "receiver"))
    parser.add_argument("--share")
    args = parser.parse_args()
    if getattr(sys, "frozen", False):
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    mode = args.mode or ("local" if args.output else None)
    if mode is None:
        print("1  本机自检（默认）\n2  跨机验收：源电脑\n3  跨机验收：接收电脑")
        selected = input("选择 1、2 或 3，直接回车选择 1：").strip() or "1"
        mode = {"1": "local", "2": "source", "3": "receiver"}.get(selected)
    started = time.monotonic()
    report = {"status": "failed", "mode": mode, "checks": []}

    def passed(message):
        print("[通过] " + message, flush=True)
        report["checks"].append(message)

    try:
        check(mode is not None, "无效选择。")
        exe = args.exe.resolve()
        check(exe.is_file(), f"找不到澜汐程序\n{exe}")
        check(
            shutil.which("git") is not None, "找不到 Git。安装 Git for Windows 后，关闭此窗口并重新双击验收。"
        )
        report["git"] = git(None, "--version")
        report["app_sha256"] = hash_file(exe)
        with tempfile.TemporaryDirectory(prefix="lanxi-check-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            if mode == "local":
                exchange = root / "relay"
                exchange.mkdir()
            else:
                share = args.share or input("共享目录绝对路径（两端都能读写）：")
                exchange = require_share(share)
                if mode == "source":
                    exchange /= SHARE_PREFIX + uuid.uuid4().hex[:12]
                    exchange.mkdir()
                report["shared_test_folder"] = str(exchange)
            verify(exe, root, exchange, mode, passed)
        report["status"] = "passed"
        print("\n验收通过。", flush=True)
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        subprocess.SubprocessError,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        report["error"] = str(error) or type(error).__name__
        print("\n[失败] " + report["error"], file=sys.stderr, flush=True)
    report["seconds"] = round(time.monotonic() - started, 2)
    output = args.output or Path(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    ) / "Lanxi" / "验收报告" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ".json")
    write_json(output, report)
    print(f"验收报告\n{output}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
