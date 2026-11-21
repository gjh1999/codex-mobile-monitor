"""Store one verified code snapshot per project and device in a paired workspace."""

import asyncio
import hashlib
import io
import os
import re
import shutil
import tempfile
import zlib
from pathlib import Path
from typing import Annotated
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from fastapi import Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

API = "/api/v1/personal/code-backups"
SHA = r"[0-9a-f]{64}"
Project = Annotated[str, Query(pattern=r"^[a-z0-9_-][a-z0-9_.-]{0,99}/[a-z0-9_-][a-z0-9_.-]{0,99}$")]
Device = Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
MAX_BYTES = 1024**3
RESERVE = 5 * 1024**3
MAX_DIRECTORY_BYTES = 8 * 1024**2
MAX_MEMBERS = 100_000
MAX_EXPANDED_BYTES = 4 * 1024**3
READ_CHUNK = 1024**2


class BackupReader(io.BufferedReader):
    def read(self, size=-1):
        # ZipFile reads the central directory in one call. Bound that allocation as well as member reads.
        if size > MAX_DIRECTORY_BYTES:
            raise BadZipFile("代码备份目录过大")
        data = super().read(size if size >= 0 else MAX_DIRECTORY_BYTES + 1)
        if len(data) > MAX_DIRECTORY_BYTES:
            raise BadZipFile("代码备份目录过大")
        return data


def verify_members(archive):
    members = archive.infolist()
    if len(members) > MAX_MEMBERS or sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
        raise BadZipFile("代码备份解压大小或文件数超过上限")
    for member in members:
        if member.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
            raise BadZipFile("代码备份压缩格式不受支持")
        size = 0
        with archive.open(member) as stream:
            while chunk := stream.read(READ_CHUNK):
                size += len(chunk)
        if size != member.file_size:
            raise BadZipFile("代码备份成员不完整")


def metadata(path, *, verify=False):
    with BackupReader(path.open("rb", buffering=0)) as source, ZipFile(source) as archive:
        version = archive.comment.decode("ascii")
        if not re.fullmatch(SHA, version):
            raise ValueError("代码备份版本无效")
        if verify:
            try:
                verify_members(archive)
            except (EOFError, RuntimeError, zlib.error) as error:
                raise BadZipFile("代码备份成员无法解压") from error
        info = os.fstat(source.fileno())
        return {"version": version, "size": info.st_size, "updated": info.st_mtime}


def current(path):
    try:
        return metadata(path)
    except FileNotFoundError:
        return None


def install_code_backups(app, directory, enrollment):
    validations = asyncio.Semaphore(2)

    def identity(x_lanxi_device: str = Header(default=""), authorization: str = Header(default="")):
        space = enrollment.space(x_lanxi_device, authorization)
        if not space or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", x_lanxi_device):
            raise HTTPException(403, "此设备未配对个人空间")
        return directory / hashlib.sha256(space.encode()).hexdigest(), x_lanxi_device

    user = Depends(identity)

    @app.get(API)
    def list_backups(project: Project, auth=user):
        rows = []
        for path in (auth[0] / project).glob("*.zip"):
            record = current(path)
            if record:
                rows.append({"device": path.stem, **record})
        return sorted(rows, key=lambda row: (-row["updated"], row["device"]))

    @app.put(API)
    async def upload(project: Project, previous: Annotated[str, Query(pattern=f"^(?:-|{SHA})$")],
                     sha256: Annotated[str, Query(pattern=f"^{SHA}$")], request: Request,
                     content_length: Annotated[int, Header(gt=0, le=MAX_BYTES)], auth=user):
        base, device = auth
        target = base / project / (device + ".zip")
        staged = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if shutil.disk_usage(target.parent).free < RESERVE + content_length:
                raise HTTPException(507, "服务器代码备份空间不足")
            checksum = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".upload-", delete=False) as file:
                staged = file.name
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > content_length:
                        raise HTTPException(413, "代码备份超过声明大小")
                    checksum.update(chunk)
                    file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
            if size != content_length or checksum.hexdigest() != sha256:
                raise HTTPException(422, "代码备份校验失败")
            async with validations:
                received = await run_in_threadpool(metadata, Path(staged), verify=True)
            prior = current(target)
            if (prior["version"] if prior else "-") != previous:
                raise HTTPException(409, "服务器备份已更新")
            os.replace(staged, target)
            staged = None
            return {"device": device, **received}
        except (BadZipFile, ValueError) as error:
            raise HTTPException(422, "代码备份格式无效") from error
        except OSError as error:
            raise HTTPException(503, "服务器代码备份存储暂不可用") from error
        finally:
            if staged:
                os.unlink(staged)

    @app.get(API + "/download")
    def download(project: Project, source: Device, auth=user):
        try:
            file = (auth[0] / project / (source + ".zip")).open("rb")
        except FileNotFoundError as error:
            raise HTTPException(404, "服务器没有这台设备的代码备份") from error

        def chunks():
            with file:
                while data := file.read(1024**2):
                    yield data

        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Length": str(os.fstat(file.fileno()).st_size), "Cache-Control": "no-store",
        })
