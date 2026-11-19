"""Initialize private device credentials and print an individual device's pairing details."""

import argparse
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

DEVICE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def initialize_devices(path, names):
    if len(names) != len(set(names)) or not all(DEVICE_NAME.fullmatch(name) for name in names):
        raise ValueError("设备名必须唯一，由 1 至 64 个英文、数字、连字符或下划线组成")
    space = secrets.token_hex(16)
    devices = {
        name: {"token": secrets.token_urlsafe(32), "personalSpace": space, "projects": []} for name in names
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(devices, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise ValueError("设备文件已经存在，保留现有设备令牌") from error
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def pairing(path, name, url):
    origin = urlsplit(url)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username is not None
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
    ):
        raise ValueError("填写完整 HTTPS 服务地址，例如 https://sync.example.com")
    devices = json.loads(path.read_text(encoding="utf-8"))
    device = devices.get(name)
    if not device or not device.get("personalSpace"):
        raise ValueError(f"没有登记个人设备 {name}")
    config = {
        "coordination": {"url": url.rstrip("/"), "device": name, "projects": {}},
        "personalSync": {},
    }
    return config, device["token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path("/var/lib/lanxi/devices.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("devices", nargs="+")
    show = commands.add_parser("pair")
    show.add_argument("device")
    show.add_argument("--url", required=True)
    options = parser.parse_args()
    try:
        if options.command == "init":
            initialize_devices(options.file, options.devices)
            print("已登记设备 " + "、".join(options.devices))
        else:
            config, token = pairing(options.file, options.device, options.url)
            print(json.dumps(config, ensure_ascii=False, indent=2))
            quoted = token.replace("'", "''")
            print(f"\n$env:LANXI_DEVICE_TOKEN = '{quoted}'")
            print(
                "[Environment]::SetEnvironmentVariable('LANXI_DEVICE_TOKEN', $env:LANXI_DEVICE_TOKEN, 'User')"
            )
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
