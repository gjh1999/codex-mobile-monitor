"""Install the official Codex approval hook and consume its local reminders."""

import base64
import json
import os
import re
import sys
import time
from contextlib import closing
from pathlib import Path

from handoff_sync import atomic_write
from service_lock import ServiceLock

HOOK_EVENT = "PermissionRequest"
ALERTS = "codex-approval-alerts"
MAX_AGE = 120
FILE_NAME = re.compile(r"[0-9a-f]{32}\.json")


def hook_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def hook_entry(root):
    script = root / ".local" / "codex-approval-alert.ps1"
    # EncodedCommand handles Windows paths containing spaces and shell metacharacters.
    command = "& '" + str(script).replace("'", "''") + "'"
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return script, {
        "hooks": [{
            "type": "command",
            "command": "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded,
            "timeout": 8,
            "statusMessage": "澜汐审批提醒",
        }]
    }


def user_hook_config(path):
    raw = path.read_bytes() if path.exists() else None
    config = json.loads(raw.decode("utf-8-sig")) if raw is not None else {"hooks": {}}
    if not isinstance(config, dict) or not isinstance(config.setdefault("hooks", {}), dict):
        raise ValueError("现有 Codex hooks.json 格式无法识别。")
    events = config["hooks"]
    group = events.get(HOOK_EVENT, [])
    if not isinstance(group, list) or any(not isinstance(row, dict) for row in group):
        raise ValueError("现有 Codex PermissionRequest 配置格式无法识别。")
    return config, group, raw


def installed(root, home):
    path = home / "hooks.json"
    script, entry = hook_entry(root)
    _, existing, _ = user_hook_config(path)
    return script.is_file() and entry in existing


def configure(root, home, enabled):
    path = home / "hooks.json"
    if not home.is_absolute():
        raise ValueError("Codex 用户配置目录需要使用绝对路径。")
    home.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("请在原始 Codex 配置目录中安装提醒 Hook。")
    script, entry = hook_entry(root)
    with closing(ServiceLock(home / "lanxi-approval-hook.lock")):
        config, existing, original = user_hook_config(path)
        if enabled:
            bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            script.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(script, (bundle / "scripts" / script.name).read_bytes())
        if enabled == (entry in existing):
            return
        updated = [row for row in existing if row != entry]
        if enabled:
            updated.append(entry)
        if updated:
            config["hooks"][HOOK_EVENT] = updated
        else:
            config["hooks"].pop(HOOK_EVENT, None)
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError("Codex Hook 配置刚刚被修改，请重试。")
        atomic_write(path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def take_alerts(root):
    inbox = root / ".local" / ALERTS
    alerts = []
    now = time.time()
    for path in inbox.glob("*.json"):
        if not FILE_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            info = path.stat()
            if not path.is_file():
                continue
            if 0 <= now - info.st_mtime <= MAX_AGE and info.st_size <= 4096:
                try:
                    row = json.loads(path.read_bytes())
                except ValueError:
                    row = None
                if isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ("project", "tool", "thread")):
                    alerts.append({k: " ".join(row[k].split())[:160] for k in ("project", "tool", "thread")})
            path.unlink(missing_ok=True)
        except OSError:
            continue
        if len(alerts) == 12:
            break
    return alerts
