"""Exercise a paired coordinator from one desktop, without sending user content."""

import json
import re
import time
import uuid
from http.client import HTTPException
from urllib.error import HTTPError

from code_backups import CodeBackups
from code_sync import github_id
from coordination_client import PersonalTaskLocks
from personal_relay import Changed, EMPTY, RemoteRecords, digest
from portable_update import endpoint, release_fields


class CheckFailed(ValueError):
    """A diagnostic message safe to include in a user-visible report."""


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def error_detail(error):
    if isinstance(error, CheckFailed):
        return str(error)
    if isinstance(error, Changed):
        return "测试记录已变化，请稍后重试。"
    cause = error
    while cause is not None:
        if isinstance(cause, HTTPError):
            cause.close()
            return f"服务器返回 HTTP {cause.code}。"
        cause = cause.__cause__
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "服务器响应格式有误。"
    return f"无法完成请求（{type(error).__name__}）。"


def record_roundtrip(store):
    # The relay keeps tombstones. Reuse a private per-device slot across checks.
    key = "verification/diagnostics-" + digest(store.client.device.encode())
    previous = store.index().get(key, EMPTY)
    if previous != EMPTY:
        pending = json.loads(store.read(key))
        require(time.time() - pending["started"] > 120, "本机已有服务器读写测试，请稍后重试。")
    payload = json.dumps({"started": time.time(), "nonce": uuid.uuid4().hex}).encode()
    revision = digest(payload)
    try:
        store.put(key, previous, payload)
        require(store.read(key) == payload, "回读内容与本轮随机测试数据不一致。")
    finally:
        # A request can reach the server even when the client times out. Only
        # our exact revision may be cleared, including when a retry races us.
        try:
            store.put(key, revision, None)
        except Changed:
            if store.index().get(key, EMPTY) == revision:
                raise
    require(store.index().get(key, EMPTY) != revision, "服务器仍保留本轮测试数据。")
    return "已通过本机设备认证，写入、回读并清除本轮随机内容。"


def server_checks(client, emit, project=None):
    """Report independent checks through emit(label, passed|failed|skipped, detail)."""
    def run(label, action):
        try:
            detail = action()
        except (OSError, HTTPException, ValueError, TypeError, KeyError, RuntimeError) as error:
            emit(label, "failed", error_detail(error))
        else:
            emit(label, "passed", detail)

    store = RemoteRecords(client)
    run("服务器认证读写", lambda: record_roundtrip(store))

    def owners():
        rows = PersonalTaskLocks(client).request()
        require(isinstance(rows, dict) and isinstance(rows.get("projects"), dict)
                and isinstance(rows.get("tasks"), dict), "项目占用清单格式有误。")
        return f"只读查询成功：{len(rows['projects'])} 个项目占用，{len(rows['tasks'])} 条任务占用。"

    run("项目与任务占用", owners)
    active = None

    def catalog():
        nonlocal active
        rows = store.index()
        require(isinstance(rows, dict) and all(
            isinstance(key, str) and isinstance(rev, str) and (rev == EMPTY or re.fullmatch("[0-9a-f]{64}", rev))
            for key, rev in rows.items()), "个人同步索引格式有误。")
        active = {key for key, rev in rows.items() if rev != EMPTY}
        return (f"只读查询成功：{sum(k.startswith('repo/') for k in active)} 个 Git 仓库，"
                f"{sum(k.startswith('folder-projects/') for k in active)} 个普通项目映射。")

    run("服务器项目清单", catalog)
    backup_project = project
    if active is not None:
        identities = [project] if project else []
        if project and project.startswith(".folder/"):
            from plain_projects import CATALOG, folder_id
            key = CATALOG + project.split("/", 1)[1]
            backup_project = None
            if key in active:
                def mapping():
                    nonlocal backup_project
                    entry = json.loads(store.read(key))
                    relative = entry["relativePath"]
                    require(isinstance(relative, str) and folder_id(relative) == project, "普通项目映射摘要不匹配。")
                    remote = entry.get("githubProject")
                    if remote and github_id("https://github.com/" + remote) == remote:
                        identities.append(remote)
                        backup_project = remote
                    return f"已登记 {relative}" + (f"，GitHub 仓库 {remote}。" if backup_project else "。")
                run("服务器普通项目映射", mapping)
            else:
                emit("服务器普通项目映射", "skipped", "尚未找到这个相对路径的登记。请在来源机完成一轮合流。")
        scopes = [identity + "/" for identity in identities] or [""]
        # A grown session can have both a legacy snapshot and a newer chunk manifest.
        names = {k.rsplit("/", 1)[-1] for k in active
                 if any(k.startswith(("sessions/" + scope, "session-manifests/" + scope)) for scope in scopes)}
        emit("已完成会话索引", "passed", f"只读查询{'当前项目' if project else '当前工作区'}索引，共 {len(names)} 条有效会话。")
        for label, prefix in (("项目记忆索引", "notes/"),
                              ("大文件清单索引", "large-inventory/"), ("吉光片羽索引", "notebook/")):
            selected = [""] if prefix == "notebook/" else scopes
            count = sum(any(k.startswith(prefix + scope) for scope in selected) for k in active)
            context = "当前项目" if selected != [""] else "当前工作区"
            emit(label, "passed", f"只读查询{context}索引，共 {count} 条有效记录。")
        peers = sorted(k for k in active if k.startswith("codex-live/"))
        if peers:
            def approvals():
                own = "codex-live/" + client.device
                row = json.loads(store.read(own if own in active else peers[0]))
                require(isinstance(row, dict) and row.get("v") == 1 and
                        all(isinstance(row.get(k), list) for k in ("approvals", "receipts", "decisions")),
                        "共享审批快照格式有误。")
                return "已只读核对一份设备快照中的审批、决定和回执列表。"
            run("共享审批快照", approvals)
        else:
            emit("共享审批快照", "skipped", "当前工作区没有可读取的设备快照。")
    else:
        emit("会话、记忆、大文件、随记和审批索引", "skipped", "读取服务器项目清单失败。")

    def backups():
        selected = backup_project
        if not selected:
            registered = sorted(k[5:] for k in (active or ()) if k.startswith("repo/")
                                and github_id("https://github.com/" + k[5:]) == k[5:])
            selected = registered[0] if registered else "lanxi-diagnostics/fixture"
        rows = CodeBackups(client).list(selected)
        require(isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("device"), str)
                and isinstance(row.get("version"), str) and re.fullmatch("[a-f0-9]{64}", row["version"])
                and type(row.get("size")) is int and row["size"] > 0 for row in rows), "备份清单格式有误。")
        return f"只读查询 {selected} 的备份目录，共 {len(rows)} 份副本。"

    def manifest():
        with endpoint(client) as response:
            raw = response.read(4097)
        require(len(raw) <= 4096, "更新清单超过长度限制。")
        version, _, size = release_fields(json.loads(raw))
        return f"已读取 R{version} 清单，版本、大小（{size} 字节）及 SHA-256 字段有效。"

    if project and project.startswith(".folder/") and (not backup_project or backup_project == project):
        emit("服务器代码备份目录", "skipped", "当前查询普通目录的会话通道。代码备份按 GitHub 仓库登记。")
    else:
        run("服务器代码备份目录", backups)
    run("私人更新清单", manifest)
