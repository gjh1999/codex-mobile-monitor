"""Exchange a paired device invitation and persist this workstation's credentials."""

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from coordination_client import ControlClient, device_token

PREFIX = "LANXI1."
SETUP_PREFIX = "LANXI2."
MAX_CODE = 12000
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")


def public_access(url, certificate=""):
    if not isinstance(url, str) or not isinstance(certificate, str):
        raise ValueError("配对入口或服务器证书格式无效。")
    address = urlsplit(url)
    if (address.scheme != "https" or not address.hostname or address.username is not None
            or address.path not in ("", "/") or address.query or address.fragment):
        raise ValueError("配对入口必须是 HTTPS 地址。")
    return {"url": url.rstrip("/"), **({"trustedCertificate": certificate} if certificate else {})}


def encode_invite(access, secret, *, prefix=PREFIX):
    body = json.dumps({**access, "code": secret}, separators=(",", ":")).encode()
    return prefix + base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def encode_setup(access, secret):
    return encode_invite(access, secret, prefix=SETUP_PREFIX)


def decode_invite(value, *, prefix=PREFIX):
    value = value.strip()
    try:
        if len(value) > MAX_CODE or not value.startswith(prefix):
            raise ValueError
        encoded = value.removeprefix(prefix)
        body = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
        secret = body["code"]
        if not SECRET.fullmatch(secret):
            raise ValueError
        return public_access(body["url"], body.get("trustedCertificate", "")), secret
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("配对码格式不正确，请粘贴完整配对码。") from error


def post(client, operation, payload=None):
    headers = {"Content-Type": "application/json"}
    if client.token:
        headers.update(Authorization="Bearer " + client.token)
        headers["X-Lanxi-Device"] = client.device
    request = Request(
        client.url + "/api/v1/pairing/" + operation,
        data=json.dumps(payload or {}).encode(), headers=headers,
    )
    try:
        with client.opener.open(request, timeout=8) as response:
            return json.loads(response.read(4096))
    except HTTPError as error:
        error.close()
        if error.code == 403:
            message = {
                "redeem": "配对码已过期或已使用。请重新生成。",
                "join-setup": "双机设置文件无效或已过期，请重新生成。",
            }.get(operation, "本机的配对凭据未获服务器认可。")
        elif error.code == 409 and operation == "join-setup":
            message = "此设置文件已用于两台电脑，或当前设备与已有申请冲突。"
        elif error.code == 404:
            message = "协调服务未提供配对入口，请使用支持配对向导的版本。"
        else:
            message = "协调服务暂时无法完成配对，请稍后重试。"
        raise ValueError(message) from error
    except (URLError, TimeoutError, OSError) as error:
        raise ValueError("无法安全连接协调服务，请检查网络和配对码中的服务器证书。") from error


def create_invite(coordination):
    access = public_access(coordination["url"], coordination.get("trustedCertificate", ""))
    client = ControlClient(coordination, device_token(coordination))
    result = post(client, "invite")
    return encode_invite(access, result["code"])


def join(root, invitation, folder, *, waiting=None, cancel=None):
    from handoff_sync import atomic_write
    from model_groups import protect_key
    from project_library import set_project_root

    automatic = invitation.strip().startswith(SETUP_PREFIX)
    access, code = decode_invite(invitation, prefix=SETUP_PREFIX if automatic else PREFIX)
    config_path = root / "local-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}
    if not isinstance(config, dict) or not folder.is_dir():
        raise ValueError("请确认本机配置正确，并选择可用的项目母目录。")
    # Keep the same device credentials for retries until the invitation expires.
    digest = hashlib.sha256(code.encode()).hexdigest()
    pending_path = root / ".local" / "pending-cloud-pairing.json"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending = json.loads(pending_path.read_bytes()) if pending_path.is_file() else {}
    if pending.get("codeHash") != digest or pending.get("access") != access:
        token = secrets.token_urlsafe(32)
        pending = {
            "codeHash": digest, "access": access, "device": "lanxi-" + secrets.token_hex(12),
            "protectedToken": base64.b64encode(protect_key(token.encode())).decode("ascii"),
        }
        atomic_write(pending_path, json.dumps(pending).encode())
    else:
        token = device_token(pending)
    projects = (config.get("coordination") or {}).get("projects", {})
    coordination = {
        **access, "device": pending["device"], "projects": projects, "protectedToken": pending["protectedToken"],
    }
    client = ControlClient(coordination, None)
    claim = {"code": code, "device": client.device, "token": token}
    if automatic:
        deadline = time.monotonic() + 1800
        while True:
            if cancel is not None and cancel.is_set():
                return False
            result = post(client, "join-setup", claim)
            if result.get("state") == "paired":
                break
            if result.get("state") != "waiting" or result.get("device") != client.device:
                raise ValueError("服务器未确认本机申请，请重试。")
            if time.monotonic() >= deadline:
                raise ValueError("等待另一台电脑超时，请重新生成双机设置文件。")
            if waiting is not None:
                waiting()
                waiting = None
            if cancel is None:
                time.sleep(2)
            else:
                cancel.wait(2)
    else:
        result = post(client, "redeem", claim)
    if result.get("device") != client.device:
        raise ValueError("服务器未确认本机配对，请重试。")
    set_project_root(root, folder)
    settings = {**(config.get("personalSync") or {}), "exchange": None, "codeOnly": False, "memory": True}
    config.update(coordination=coordination, personalSync=settings)
    atomic_write(config_path, json.dumps(config, ensure_ascii=False).encode())
    return True
