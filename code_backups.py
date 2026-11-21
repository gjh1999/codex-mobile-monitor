"""Back up saved project files through the authenticated personal workspace."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from code_sync import SyncIssue, auto_path, is_large, run, verify_checkout
from personal_memory import safe_path


def stamp(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or is_large(info.st_size)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return None
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


def saved_files(repo, memory):
    tracked = set(run(repo, "ls-files", "--cached", "-z").stdout.split(b"\0")) - {b""}
    new = set(run(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")) - {b""}
    files = {}
    memory = memory.rstrip("/").casefold()
    for raw in sorted(tracked | new):
        name = raw.decode()
        normalized = name.casefold()
        if normalized == memory or normalized.startswith(memory + "/"):
            continue
        try:
            path = safe_path(repo, name)
            saved = stamp(path)
        except (FileNotFoundError, ValueError):
            continue
        if saved and auto_path(name, raw in tracked, path):
            files[raw] = name, path, saved
    if not files:
        return []
    attributes = run(repo, "check-attr", "-z", "--stdin", "filter",
                     data=b"\0".join(files) + b"\0").stdout.split(b"\0")
    return [files[raw] for raw, _, setting in zip(attributes[::3], attributes[1::3], attributes[2::3])
            if setting in (b"unspecified", b"unset")]


class CodeBackups:
    def __init__(self, client):
        self.client = client

    def open(self, project, *, action="", method="GET", data=None, extra=None, size=None):
        query = urlencode({"project": project, **(extra or {})})
        headers = {"Authorization": "Bearer " + (self.client.token or ""), "X-Lanxi-Device": self.client.device}
        if size is not None:
            headers.update({"Content-Length": str(size), "Content-Type": "application/zip"})
        request = Request(self.client.url + "/api/v1/personal/code-backups" + action + "?" + query,
                          method=method, data=data, headers=headers)
        try:
            return self.client.opener.open(request, timeout=30)
        except HTTPError as error:
            error.close()
            messages = {
                403: "此设备无权访问服务器代码备份", 404: "服务器尚无此备份或尚未启用代码备份",
                409: "服务器备份刚刚更新，将在下一轮重试", 413: "代码备份超过服务器大小限制",
                422: "服务器拒绝代码备份，请核对大小和格式", 507: "服务器代码备份空间不足",
            }
            raise SyncIssue(messages.get(error.code, "服务器代码备份暂不可用")) from error

    def list(self, project):
        with self.open(project) as response:
            return json.load(response)

    def sync(self, repo, project, memory, stop):
        verify_checkout(repo, project)
        files = saved_files(repo, memory)
        if stop.is_set():
            return "未完成"
        rows = self.list(project)
        previous = next((row["version"] for row in rows if row["device"] == self.client.device), "-")
        if not files and previous == "-":
            return "没有可备份的代码"
        source_index = json.dumps([(name, saved) for name, _, saved in files], ensure_ascii=False).encode()
        version = hashlib.sha256(source_index).hexdigest()
        if version == previous:
            return "已备份"
        with tempfile.TemporaryFile() as staged:
            with ZipFile(staged, "w", ZIP_DEFLATED, compresslevel=3, strict_timestamps=False) as archive:
                archive.comment = version.encode("ascii")
                for name, path, saved in files:
                    if stop.is_set():
                        return "未完成"
                    if stamp(safe_path(repo, name)) != saved:
                        raise SyncIssue("项目文件正在写入，下一轮将重试云端备份")
                    archive.write(path, name)
                    if staged.tell() > 1024**3:
                        raise SyncIssue("代码备份压缩后超过 1 GiB")
                    if stamp(safe_path(repo, name)) != saved:
                        raise SyncIssue("项目文件正在写入，下一轮将重试云端备份")
            size = staged.tell()
            if size > 1024**3:
                raise SyncIssue("代码备份压缩后超过 1 GiB")
            staged.seek(0)
            checksum = hashlib.file_digest(staged, "sha256").hexdigest()
            staged.seek(0)
            if stop.is_set():
                return "未完成"
            with self.open(project, method="PUT", data=staged, size=size,
                           extra={"previous": previous, "sha256": checksum}) as response:
                if json.load(response).get("version") != version:
                    raise SyncIssue("服务器未确认此次代码备份")
        return "已备份"

    def download(self, project, source, destination):
        destination = Path(destination)
        temporary = None
        try:
            with self.open(project, action="/download", extra={"source": source}) as response, \
                    tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".lanxi-backup-", delete=False) as file:
                temporary = Path(file.name)
                shutil.copyfileobj(response, file, length=1024**2)
                if file.tell() != int(response.headers["Content-Length"]):
                    raise SyncIssue("下载的服务器备份不完整")
            with ZipFile(temporary) as archive:
                for member in archive.infolist():
                    safe_path(destination.parent, member.filename)
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise SyncIssue("服务器备份包含目录链接")
                if archive.testzip() is not None:
                    raise SyncIssue("下载的服务器备份未通过校验")
            os.replace(temporary, destination)
        except BadZipFile as error:
            raise SyncIssue("下载的服务器备份未通过校验") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
