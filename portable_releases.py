"""Serve an administrator-published Windows package to paired devices."""

import json
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Path
from fastapi.responses import FileResponse, JSONResponse

from portable_update import release_fields


def install_releases(app, directory, enrollment):
    def authorize(x_lanxi_device: str = Header(default=""), authorization: str = Header(default="")):
        if not enrollment.space(x_lanxi_device, authorization):
            raise HTTPException(403, "此设备未配对个人空间")

    def manifest():
        try:
            data = json.loads((directory / "latest.json").read_bytes())
            release_fields(data)
            return data
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise HTTPException(404, "暂无可用版本") from error

    secured = [Depends(authorize)]

    @app.get("/api/v1/releases/windows", dependencies=secured)
    def latest():
        data = manifest()
        return JSONResponse({key: data[key] for key in ("version", "sha256", "size")},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/releases/windows/{version}", dependencies=secured)
    def download(version: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,5}$")]):
        path = directory / f"Lanxi-R{version}-Windows-Private.zip"
        if not path.is_file():
            raise HTTPException(404, "版本包不存在")
        return FileResponse(path, media_type="application/zip")
