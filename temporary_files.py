"""Run approved temporary file exchanges through the personal cloud connection."""

import hashlib
import json
import os
import queue
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from code_sync import LIMIT, auto_path, run, value
from compute_artifacts import CHUNK, append_block
from handoff import local_path
from large_inventory import registered
from lfs_assets import file_size, matches


class TransferProblem(ValueError):
    pass


class TransferClient:
    def __init__(self, client):
        self.client = client

    def request(self, operation="", method="GET", body=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        request = Request(self.client.url + "/api/v1/temporary-files" + operation, method=method, data=raw,
                          headers={"Authorization": "Bearer " + self.client.token, "X-Lanxi-Device": self.client.device,
                                   "Content-Type": "application/octet-stream" if isinstance(body, bytes) else "application/json"})
        try:
            with self.client.opener.open(request, timeout=20) as response:
                data = response.read(CHUNK + 1)
                if len(data) > CHUNK:
                    raise TransferProblem("服务器返回的数据块超过上限。")
                return json.loads(data) if response.headers.get_content_type() == "application/json" else data
        except HTTPError as error:
            with error:
                if error.code in (409, 507):
                    detail = json.loads(error.read(4096)).get("detail", "")
                    raise TransferProblem(detail) from error
                raise TransferProblem("暂时无法访问大文件借用服务。") from error

    def upload(self, key, part, path, stop):
        size = file_size(path)
        if size is None:
            raise TransferProblem("本机缺少要上传的文件。")
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if file_size(path) != size:
                raise TransferProblem("文件正在修改，请保存后重试。")
            route = f"/{key}/{part}"
            offset = self.request(route, "POST", {"sha256": checksum, "size": size})["offset"]
            while offset < size and not stop.is_set():
                stream.seek(offset)
                block = stream.read(min(CHUNK, size - offset))
                if not block:
                    raise TransferProblem("上传过程中本机文件已改变。")
                offset = self.request(f"{route}?offset={offset}", "PUT", block)["offset"]

    def download(self, key, part, info, staging, stop):
        sha, size = info["sha256"], info["size"]
        local = file_size(staging) or 0
        if local > size:
            staging.unlink()
            local = 0
        while local < size:
            if stop.is_set():
                return False
            block = self.request(f"/{key}/{part}?offset={local}")
            if len(block) != min(CHUNK, size - local):
                raise TransferProblem("收到的文件分块大小有误。")
            append_block(staging, block)
            local += len(block)
        if not staging.exists():
            staging.touch(exist_ok=False)
        if not matches(staging, sha, size):
            staging.unlink()
            raise TransferProblem("下载校验失败，下一轮会重试。")
        return True

    def receipt(self, key, event, info):
        self.request(f"/{key}/receipt", "POST", {"event": event, "sha256": info["sha256"]})


class FileExchange:
    def __init__(self, root, personal):
        self.root = root
        self.personal = personal
        self.commands = queue.Queue()
        self.rows = []
        self.errors = {}
        self.wake = threading.Event()

    def request(self, project, source, path):
        self.commands.put({"project": project, "source": source, "path": path})
        self.wake.set()

    def submit(self, key):
        path = self.marker(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.wake.set()

    def marker(self, key):
        return self.root / ".local" / "file-returns" / key

    def source_allows(self, project, directory, name, target):
        if not auto_path(name, True, target):
            return False
        if name in registered(self.root).get(project, []):
            return True
        return run(directory, "ls-files", "--error-unmatch", "--", name, check=False).returncode == 0

    def operation(self, client, row, stop):
        key, project, name, state = (row[k] for k in ("key", "project", "name", "state"))
        if state in ("completed", "conflict"):
            self.marker(key).unlink(missing_ok=True)
            return
        directory = self.personal.snapshot()["projects"].get(project)
        if not directory:
            raise TransferProblem("项目尚未在本机准备好。")
        directory = Path(directory)
        target = local_path(directory, name)
        prefix = Path(value(directory, "rev-parse", "--absolute-git-dir")) / "lanxi-file-exchange" / key
        prefix.mkdir(parents=True, exist_ok=True)
        original, edited = row["original"], row["edited"]
        if row["source"] == client.client.device:
            if state in ("requested", "uploading"):
                if not self.source_allows(project, directory, name, target):
                    raise TransferProblem("来源机只提供 Git 跟踪或者手动登记的大文件。")
                if (file_size(target) or 0) < LIMIT:
                    raise TransferProblem("来源机缺少已登记的原文件。")
                client.upload(key, "original", target, stop)
            elif state == "returned":
                self.apply(client, key, target, prefix, original, edited, stop)
        elif state == "ready":
            if not matches(target, original["sha256"], original["size"]):
                if file_size(target) is not None:
                    raise TransferProblem("本机已有同名但内容不同的文件，请移开后接收。")
                staging = prefix / "original.part"
                if not client.download(key, "original", original, staging, stop):
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(staging, target)
                staging.unlink()
            client.receipt(key, "downloaded", original)
        elif state in ("editing", "returning") and self.marker(key).is_file():
            client.upload(key, "edited", target, stop)

    @staticmethod
    def apply(client, key, target, directory, original, edited, stop):
        if matches(target, edited["sha256"], edited["size"]):
            client.receipt(key, "applied", edited)
            return
        backup = directory / "before-edit"
        source = backup if backup.is_file() and not target.exists() else target
        if not matches(source, original["sha256"], original["size"]):
            client.receipt(key, "conflict", edited)
            return
        staging = directory / "edited.part"
        if not client.download(key, "edited", edited, staging, stop):
            return
        if source == target:
            if not matches(target, original["sha256"], original["size"]):
                client.receipt(key, "conflict", edited)
                return
            if backup.exists():
                raise TransferProblem("本机已有这次交接的恢复副本，请查看原文件。")
            target.rename(backup)
            if not matches(backup, original["sha256"], original["size"]):
                if not target.exists():
                    os.link(backup, target)
                client.receipt(key, "conflict", edited)
                return
        try:
            os.link(staging, target)
        except OSError:
            if not target.exists():
                os.link(backup, target)
            raise
        staging.unlink()
        client.receipt(key, "applied", edited)

    def run(self, stop):
        pending = None
        while not stop.is_set():
            try:
                locks = self.personal.task_locks()
                if locks and locks.client.token:
                    client = TransferClient(locks.client)
                    if pending is None and not self.commands.empty():
                        pending = self.commands.get_nowait()
                    if pending:
                        client.request("", "POST", pending)
                        pending = None
                    rows = client.request()
                    self.rows = rows
                    errors = {}
                    for row in rows:
                        if stop.is_set():
                            break
                        try:
                            self.operation(client, row, stop)
                        except (ValueError, OSError, TypeError, KeyError) as error:
                            errors[row["key"]] = str(error)
                    self.errors = errors
            except (OSError, ValueError, KeyError):
                self.errors = {"连接": "借用服务暂不可用，稍后重试。"}
            self.wake.wait(5)
            self.wake.clear()
