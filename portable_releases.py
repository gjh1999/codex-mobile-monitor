"""Serve an administrator-published Windows package to paired devices."""

import json
import re
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Path
from fastapi.responses import FileResponse, JSONResponse

from portable_update import release_fields
from device_enrollment import sha


def install_releases(app, directory, enrollment):
    def authorize(x_lanxi_device: str = Header(default=""), authorization: str = Header(default="")):
        space = enrollment.space(x_lanxi_device, authorization)
        if not space:
            raise HTTPException(403, "此设备未配对个人空间")
        return space

    def check_scope(version, space):
        # Beginning with R33, a private package may carry workspace enrollment rights.
        if int(version) < 33:
            return
        try:
            expected = (directory / f"Lanxi-R{version}-Windows-Private.scope").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise HTTPException(404, "暂无可用版本") from error
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or not secrets.compare_digest(expected, sha(space)):
            raise HTTPException(403, "此版本仅供指定工作区使用")

    def manifest():
        try:
            data = json.loads((directory / "latest.json").read_bytes())
            release_fields(data)
            return data
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise HTTPException(404, "暂无可用版本") from error

    @app.get("/api/v1/releases/windows")
    def latest(space: str = Depends(authorize)):
        data = manifest()
        check_scope(data["version"], space)
        return JSONResponse({key: data[key] for key in ("version", "sha256", "size")},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/releases/windows/{version}")
    def download(version: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,5}$")], space: str = Depends(authorize)):
        check_scope(version, space)
        path = directory / f"Lanxi-R{version}-Windows-Private.zip"
        if not path.is_file():
            raise HTTPException(404, "版本包不存在")
        return FileResponse(path, media_type="application/zip")
