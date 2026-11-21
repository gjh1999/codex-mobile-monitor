"""Exercise the packaged desktop, the paired server and project session diagnostics."""

import argparse
import ctypes
import hashlib
import json
import os
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
from urllib.error import HTTPError

from code_sync import LIMIT, github_id, run
from coordination_client import ControlClient, device_token
from desktop_storage import user_data_root
from personal_relay import Records
from local_feature_checks import local_feature_checks
from server_diagnostics import server_checks

REMOTE = "https://github.com/lanxi-check/fixture.git"
PROJECT = "lanxi-check/fixture"
PROJECT_FOLDER = Path("客户甲/工具组/项目 A")
MEMORY = Path("notes/Codex")
BIG_FILE = "fixture-large.bin"
BELOW_LIMIT = "fixture-below-limit.bin"
TIMEOUT = 90
MENU = {
    "1": ("local", "本机自检（默认）"),
    "2": ("server", "服务器测试"),
    "3": ("project", "项目会话诊断"),
}


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


def source_fixture(exchange, repo, profile):
    bare = exchange / "remote.git"
    git(None, "init", "--bare", "-b", "main", str(bare))
    git(bare, "config", "uploadpack.allowFilter", "true")
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "remote", "add", "origin", REMOTE)
    write(repo / "app.py", "# source change\n")
    for name, size in ((BIG_FILE, LIMIT), (BELOW_LIMIT, LIMIT - 1)):
        with (repo / name).open("wb") as stream:
            stream.truncate(size)
    git(repo, "add", "app.py", BIG_FILE, BELOW_LIMIT)
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
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    inherited.update(GIT_CONFIG_GLOBAL=str(root / "test.gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    with patch.dict(os.environ, inherited, clear=True):
        git(None, "config", "--global", "user.name", "Lanxi verification")
        git(None, "config", "--global", "user.email", "fixture@example.invalid")
        git(None, "config", "--global", f"url.{bare.as_uri()}.insteadOf", REMOTE)
        git(None, "config", "--global", "safe.directory", bare.as_posix())
        yield


def prepare_runtime(exe, runtime, exchange, source=None):
    runtime.mkdir()
    binary = runtime / exe.name
    shutil.copy2(exe, binary)
    profile = runtime / "codex"
    profile.mkdir()
    projects = [{"remote": REMOTE, "path": str(source), "memoryPath": MEMORY.as_posix()}] if source else []
    parent = runtime.parent / ("source-projects" if source else "receiver-projects")
    parent.mkdir(exist_ok=True)
    write_json(runtime / "project-library.json", {"root": str(parent)})
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


def verify_local(exe, root, report):
    exchange = root / "relay"
    exchange.mkdir()
    source, receiver = root / "source-projects" / PROJECT_FOLDER, root / "receiver"
    source_runtime = root / "source"
    note_key = f"notes/{PROJECT}/decision.md"
    database = Records(exchange / "lanxi-personal.sqlite", "personal")
    with isolated_git(root, exchange):
        source_profile = prepare_runtime(exe, source_runtime, exchange, source)
        fixture = source_fixture(exchange, source, source_profile)
        with running(exe, source_runtime) as proc:
            session_key = f"sessions/{PROJECT}/{session_path(fixture).name}"
            wait_for(
                "源机发布项目和记忆",
                lambda: database.read(note_key) == b"source note\n" and database.read(session_key) is not None,
                proc,
            )
            report("源机启动并发布项目记忆")

        profile = prepare_runtime(exe, receiver, exchange)
        target = root / "receiver-projects" / PROJECT_FOLDER
        note = target / MEMORY / "decision.md"
        with running(exe, receiver) as proc:
            wait_for("接收机自动克隆并收到同路径项目记忆", lambda: read_is(note, "source note"), proc)
            check(read_is(target / "app.py", "# source change"), "接收端克隆的代码内容无效。")
            report("从空工作区按母目录相对路径克隆，项目记忆落在 notes/Codex/decision.md")
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
                "达到 100 MiB 的测试文件进入了接收端工作区或 Git 对象库。",
            )
            check((target / BELOW_LIMIT).is_file() and (target / BELOW_LIMIT).stat().st_size == LIMIT - 1,
                  "小于 100 MiB 的测试文件没有完整检出。")
            report("小于 100 MiB 的文件已检出，达到上限的文件仍留在源端")
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
            receiver_head = git(target, "rev-parse", "HEAD")
            report("接收机自动提交和回传代码、项目记忆")
        with running(exe, source_runtime) as proc:
            wait_for(
                "源机收到回传的代码和记忆",
                lambda: (
                    read_is(source / "app.py", "# receiver change")
                    and read_is(source / MEMORY / "decision.md", "receiver note")
                    and receiver_head == git(source, "rev-parse", "HEAD")
                ),
                proc,
            )
            report("源端收到回传，双方 Git 提交一致")


def cloud_client(data_dir):
    try:
        config = json.loads((data_dir / "local-config.json").read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise RuntimeError("本机尚未配对。请从澜汐托盘选择「跨机接续 → 使用配对码连接」，再运行测试。") from error
    settings = config.get("personalSync")
    override = data_dir / "personal-sync.json"
    if override.is_file():
        settings = {**(settings or {}), **json.loads(override.read_text(encoding="utf-8-sig"))}
    cfg = config.get("coordination") or {}
    check(bool(cfg), "本机尚未配置阿里云配对，请先在澜汐中完成配对。")
    check(cfg.get("protectedToken") or (
        isinstance(settings, dict) and not settings.get("exchange") and not settings.get("codeOnly")
        and settings.get("memory", True)
    ), "请先在本机配置澜汐协调服务并启用个人记忆同步。")
    token = device_token(cfg)
    check(token, "本机缺少协调服务的设备令牌，请检查配对。")
    client = ControlClient(cfg, token)
    with client.opener.open(client.url + "/healthz", timeout=5) as response:
        check(json.load(response) == {"status": "ready"}, "协调服务尚未就绪。")
    return client


def cloud_roundtrip(mode, data_dir, code, passed):
    client = cloud_client(data_dir)
    passed("本机已连接协调服务")
    store = RemoteRecords(client)
    source = mode == "cloud-source"
    code = uuid.uuid4().hex if source else (code or input("源电脑显示的测试码：")).strip()
    check(re.fullmatch("[0-9a-f]{32}", code), "测试码格式错误。")
    key = "verification/" + code
    if not source:
        offer = store.read(key)
        request = json.loads(offer)
        check(set(request) == {"from", "code"} and request["code"] == code, "这组验收已结束或测试码错误。")
        check(request["from"] != client.device, "两端使用了相同设备配置。")
        reply = {**request, "reply": client.device}
        store.put(key, digest(offer), json.dumps(reply).encode())
        passed("已读取源设备的测试记录，并通过本机设备令牌回传")
        return
    request = {"from": client.device, "code": code}
    offer = json.dumps(request).encode()
    store.put(key, EMPTY, offer)
    passed("源设备令牌已验证，已写入独立测试记录")
    print(f"请在另一台电脑的测试窗口输入测试码 {code}，然后点「回传测试」。", flush=True)
    current = offer
    try:
        deadline = time.monotonic() + 20 * 60
        while time.monotonic() < deadline:
            current = store.read(key)
            if current != offer:
                reply = json.loads(current)
                check(reply.get("from") == client.device and reply.get("code") == code
                      and reply.get("reply") and reply["reply"] != client.device, "收到的回执与本轮不符。")
                passed("已从另一设备收到同一测试码的回执")
                return
            time.sleep(2)
        raise TimeoutError("等待另一台设备回传超时。")
    finally:
        store.put(key, digest(current), None)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=(*[mode for mode, _ in MENU.values()], "diagnostics"))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--code")
    args = parser.parse_args()
    if getattr(sys, "frozen", False):
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    mode = args.mode or ("local" if args.output else None)
    if mode is None:
        for number, (_, label) in MENU.items():
            print(f"{number}  {label}")
        selected = input("选择模式，直接回车选择 1：").strip() or "1"
        mode = MENU[selected][0] if selected in MENU else None
    started = time.monotonic()
    report = {"status": "failed", "mode": mode, "checks": []}

    def passed(message):
        print("[通过] " + message, flush=True)
        report["checks"].append(message)

    try:
        check(mode is not None, "无效选择。")
        exe = args.exe.resolve()
        check(exe.is_file(), f"找不到澜汐程序\n{exe}")
        report["app_sha256"] = hash_file(exe)
        data_dir = args.data_dir or user_data_root()
        if mode in ("cloud-source", "cloud-receiver"):
            cloud_roundtrip(mode, data_dir, args.code, passed)
        else:
            check(shutil.which("git") is not None, "本机自检需要 Git for Windows。")
            report["git"] = git(None, "--version")
            with tempfile.TemporaryDirectory(prefix="lanxi-check-", ignore_cleanup_errors=True) as temp:
                verify_local(exe, Path(temp), passed)
            if mode == "diagnostics":
                PersonalTaskLocks(cloud_client(data_dir)).request()
                passed("已连接协调服务并验证本机配对及项目占用接口")
        report["status"] = "passed"
        print("\n验收通过。", flush=True)
    except HTTPError as error:
        report["error"] = f"协调服务返回 HTTP {error.code}，请检查设备配对和测试码。"
        print(report["error"], file=sys.stderr)
        error.close()
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
