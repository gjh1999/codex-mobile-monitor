"""Exercise recent desktop features using only disposable local data."""

import hashlib
import io
import json
import queue
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from automation import Approvals
from code_backups import CodeBackups
from code_sync import LIMIT, run
from coordination import Conflict, Ownership
from coordination_client import ControlDenied
from ide_activity import close_queue, report
from large_inventory import register, sync_inventory
from notebook_store import NotebookStore, sync_notebook
from personal_relay import Records


def require(condition, message):
    if not condition:
        raise ValueError(message)


def notebook(root):
    source, receiver = root / "source", root / "receiver"
    source.mkdir()
    receiver.mkdir()
    relay = Records(root / "records.sqlite3", "diagnostics")
    first = NotebookStore(source / "lanxi-notebook.sqlite3")
    note = first.save("note", None, "测试随记")
    first.assign("note", note, "自检")
    first.set_filed("note", note, True)
    first.save("todo", None, "测试待办")

    def sync(folder):
        _, errors = sync_notebook(folder, relay, relay.index(), threading.Event())
        require(not errors, "随记同步失败：" + "、".join(errors))

    sync(source)
    sync(receiver)
    second = NotebookStore(receiver / first.path.name)
    notes = second.entries("note", archived=True)
    require(len(notes) == 1 and notes[0][2] == "测试随记" and second.groups("note").get(notes[0][0]) == "自检",
            "归档随记正文或手动分组未保留")
    todos = second.entries("todo")
    require(len(todos) == 1 and todos[0][2:4] == ("测试待办", 0), "待办未同步")
    second.set_filed("todo", todos[0][0], True)
    sync(receiver)
    sync(source)
    require(first.entries("todo")[0][2:4] == ("测试待办", 1), "待办完成状态未回传")
    # Entries created in the same second may have different local insertion order.
    require(second.daily_dates() and first.daily_dates() == second.daily_dates()
            and all(sorted(first.daily_entries(day), key=repr) == sorted(second.daily_entries(day), key=repr)
                    for day in second.daily_dates()),
            "每日记录不一致")
    return "隔离资料库的随记归档、分组、待办完成状态与每日记录往返一致"


def approvals(root):
    requests = Approvals()
    responses = []

    def lost_control(thread):
        raise ControlDenied("测试会话已交还")

    connection = SimpleNamespace(approval_guard=lost_control, send=responses.append)
    method = "item/commandExecution/requestApproval"
    pending = requests.add(connection, {"id": 1, "method": method,
                                        "params": {"threadId": "fixture", "command": "echo fixture"}})
    require(requests.decide_pending(pending, True) == "control_lost" and not responses,
            "控制权丢失后没有拦住同意回执")
    missing = requests.add(SimpleNamespace(send=responses.append), {"id": 2, "method": method, "params": {}})
    require(requests.decide_pending(missing, True) == "details_unavailable" and not responses,
            "缺少操作详情时没有拦住同意回执")
    require(requests.decide_pending(pending, False) == "denied"
            and responses == [{"id": 1, "result": {"decision": "decline"}}], "拒绝回执与原请求不符")
    return "隔离请求模拟控制权丢失和缺少详情，核对拦截结果与拒绝回执"


def ide_ownership(root):
    center = Ownership(root / "ownership.sqlite3")
    project = "diagnostics/example"
    observed = []

    def event(project, session, source, sequence, action):
        state = center.report(project, "local", session, source, sequence, action)
        observed.append((action, [item["source"] for item in state["humans"]]))
        if action == "open":
            try:
                center.reserve(project, "peer", "worker", "thread", state["version"])
            except Conflict:
                return
            raise ValueError("IDE 使用项目时错误地允许另一个后台写入")

    client = SimpleNamespace(event=event, url="http://127.0.0.1", device="local", paths={root: project})
    for source in ("vscode", "pycharm"):
        commands = queue.Queue()
        commands.put("close")
        report(client, close_queue(root), project, source, commands)
        require(observed[-2:] == [("open", [source]), ("close", [])], "IDE 的打开和关闭未正确改变项目占用")
    return "隔离数据库验证 VS Code / PyCharm 上报、占用时拒绝写入和关闭后释放"


def inventory(root):
    project = "diagnostics/inventory"
    repo = root / "repo"
    run(None, "init", str(repo))
    asset = repo / "sample.dat"
    with asset.open("wb") as stream:
        stream.truncate(LIMIT)
    register(root, project, repo, [asset])
    relay = Records(root / "records.sqlite3", "diagnostics")
    relay.client = SimpleNamespace(device="source")
    _, errors = sync_inventory(root, relay, {}, {project: (repo, ".codex")}, threading.Event())
    require(not errors, "大文件清单发布失败")
    payload = relay.read(f"large-inventory/{project}/source")
    require(payload and json.loads(payload)["files"] == {"sample.dat": LIMIT}, "清单未记录阈值大小")
    relay.client = SimpleNamespace(device="receiver")
    asset.unlink()
    files, errors = sync_inventory(root, relay, relay.index(), {project: (repo, ".codex")}, threading.Event())
    require(not errors and files[project]["sample.dat"]["localSize"] is None and not asset.exists(),
            "接收端未正确报告缺失的大文件")
    return "100 MiB 边界文件已进入清单，隔离接收端能识别缺失"


def backup(root):
    repo = root / "repo"
    run(None, "init", str(repo))
    project = "diagnostics/backup"
    run(repo, "remote", "add", "origin", f"https://github.com/{project}.git")
    (repo / "main.py").write_bytes(b"print('fixture')\n")
    run(repo, "add", "main.py")
    (repo / "secrets.json").write_bytes(b"{}")
    (repo / ".codex").mkdir()
    (repo / ".codex" / "note.md").write_bytes(b"fixture memory")

    # Replace only HTTP. The production client builds the ZIP and computes its digest.
    class FixtureBackups(CodeBackups):
        def open(self, project, *, method="GET", data=None, size=None, extra=None):
            if method == "GET":
                return io.BytesIO(b"[]")
            require(method == "PUT", "备份请求方法错误")
            payload = data.read()
            require(len(payload) == size and hashlib.sha256(payload).hexdigest() == extra["sha256"],
                    "备份的大小或摘要不匹配")
            with ZipFile(io.BytesIO(payload)) as archive:
                require(archive.namelist() == ["main.py"] and archive.read("main.py") == (repo / "main.py").read_bytes(),
                        "备份未保留源码，或误包含私有配置、记忆文件")
                version = archive.comment.decode("ascii")
            return io.BytesIO(json.dumps({"version": version}).encode())

    client = FixtureBackups(SimpleNamespace(device="diagnostics"))
    require(client.sync(repo, project, ".codex", threading.Event()) == "已备份", "备份未完成")
    return "本机生成测试 ZIP，校验源码、文件过滤和 SHA-256"


def local_feature_checks(root, emit):
    checks = (
        ("吉光片羽", notebook), ("审批回执", approvals), ("IDE 项目占用", ide_ownership),
        ("大文件清单", inventory), ("代码备份打包", backup),
    )
    for label, check in checks:
        try:
            with tempfile.TemporaryDirectory(dir=root) as scratch:
                detail = check(Path(scratch))
        except Exception as error:
            emit(label, "failed", str(error) or type(error).__name__)
        else:
            emit(label, "passed", detail)
