"""Versioned Windows releases authenticated by the paired HTTPS coordinator."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request
from zipfile import ZipFile

from coordination_client import device_client

VERSION = "34"
LIMIT = 160 * 1024**2


def release_fields(data):
    version, sha, size = (data[name] for name in ("version", "sha256", "size"))
    if (not re.fullmatch(r"[1-9][0-9]{0,5}", version)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or type(size) is not int or not 0 < size <= LIMIT):
        raise ValueError("更新清单格式无效。")
    return version, sha, size


def client_for(config):
    client = device_client(config)
    if client is None or not client.url.startswith("https://") or not client.token:
        raise ValueError("请先配对 HTTPS 协调服务。")
    return client


def endpoint(client, suffix=""):
    return client.opener.open(Request(client.url + "/api/v1/releases/windows" + suffix, headers={
        "Authorization": "Bearer " + client.token, "X-Lanxi-Device": client.device,
    }), timeout=20)


def latest(config):
    with endpoint(client_for(config)) as response:
        data = json.loads(response.read(4096))
    return data if int(release_fields(data)[0]) > int(VERSION) else None


def prepare(config, manifest, root):
    version, sha, expected_size = release_fields(manifest)
    destination = root / f"Lanxi-R{version}-Portable"
    executable = f"Lanxi-Glass-R{version}-Portable.exe"
    client = client_for(config)
    root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"更新目录已存在，请从该目录打开新版。\n{destination}")
    with tempfile.TemporaryDirectory(prefix=".lanxi-install-", dir=root) as temp:
        archive = Path(temp) / "package.zip"
        actual_size = 0
        checksum = hashlib.sha256()
        with endpoint(client, "/" + version) as response, archive.open("xb") as stream:
            while chunk := response.read(128 * 1024):
                actual_size += len(chunk)
                if actual_size > expected_size:
                    raise ValueError("更新包超过清单大小。")
                checksum.update(chunk)
                stream.write(chunk)
        if actual_size != expected_size or checksum.hexdigest() != sha:
            raise ValueError("更新包校验失败。")
        stage = Path(temp) / "unpacked"
        stage.mkdir()
        with ZipFile(archive) as package:
            files = package.infolist()
            names = {item.filename for item in files}
            if (len(names) != len(files) or len(files) > 24 or executable not in names
                    or sum(item.file_size for item in files) > 3 * LIMIT):
                raise ValueError("更新包文件清单无效。")
            for item in files:
                name = item.filename
                if not re.fullmatch(r"[\w.-]+", name) or name[0] == "." or name[-1] == "." or Path(name).is_reserved():
                    raise ValueError("更新包中有无效文件名。")
                with package.open(item) as source, (stage / name).open("xb") as target:
                    shutil.copyfileobj(source, target)
        stage.rename(destination)
    return destination / executable


def restart_after_exit(executable):
    import base64
    import os

    literal = "'" + str(executable).replace("'", "''") + "'"
    directory = "'" + str(executable.parent).replace("'", "''") + "'"
    script = (f"$ErrorActionPreference='Stop'; Wait-Process -Id {os.getpid()}; "
              f"Start-Process -FilePath {literal} -WorkingDirectory {directory}")
    command = base64.b64encode(script.encode("utf-16le")).decode()
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                     env=env, creationflags=subprocess.CREATE_NO_WINDOW)
