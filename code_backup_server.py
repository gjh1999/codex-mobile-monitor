"""Store one verified code snapshot per project and device in a paired workspace."""

import hashlib
import os
import re
import shutil
import tempfile
from typing import Annotated
from zipfile import BadZipFile, ZipFile

from fastapi import Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

API = "/api/v1/personal/code-backups"
SHA = r"[0-9a-f]{64}"
Project = Annotated[str, Query(pattern=r"^[a-z0-9_-][a-z0-9_.-]{0,99}/[a-z0-9_-][a-z0-9_.-]{0,99}$")]
Device = Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
MAX_BYTES = 1024**3
RESERVE = 5 * 1024**3


def metadata(source):
    with ZipFile(source) as archive:
        version = archive.comment.decode("ascii")
    if not re.fullmatch(SHA, version):
        raise ValueError("代码备份版本无效")
    info = os.fstat(source.fileno())
    return {"version": version, "size": info.st_size, "updated": info.st_mtime}


def current(path):
    try:
        with path.open("rb") as file:
            return metadata(file)
    except FileNotFoundError:
        return None


def install_code_backups(app, directory, enrollment):
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
            with open(staged, "rb") as file:
                received = metadata(file)
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
