import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_approval_alerts import configure, hook_entry, installed, take_alerts


class CodexApprovalAlertsTests(unittest.TestCase):
    def test_toggle_preserves_other_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "user codex"
            home.mkdir()
            path = home / "hooks.json"
            existing = {"description": "fixture", "hooks": {
                "PermissionRequest": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "fixture"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "other"}]}],
            }}
            path.write_text(json.dumps(existing), encoding="utf-8")
            configure(root, home, True)
            configure(root, home, True)
            self.assertTrue(installed(root, home))
            actual = json.loads(path.read_bytes())
            self.assertEqual(actual["hooks"]["PermissionRequest"], existing["hooks"]["PermissionRequest"]
                             + [hook_entry(root)[1]])
            configure(root, home, False)
            self.assertFalse(installed(root, home))
            self.assertEqual(json.loads(path.read_bytes()), existing)
            path.write_bytes(b"broken")
            with self.assertRaises(ValueError):
                configure(root, home, True)
            self.assertEqual(path.read_bytes(), b"broken")

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell hook")
    def test_hook_emits_reminder_and_no_approval_decision(self):
        with tempfile.TemporaryDirectory(prefix="澜汐 hook's path ") as directory:
            root = Path(directory)
            configure(root, root / "codex", True)
            _, entry = hook_entry(root)
            command = entry["hooks"][0]["command"].split()
            event = {"hook_event_name": "PermissionRequest", "cwd": str(root / "某项目"),
                     "session_id": "fixture", "tool_name": "Bash",
                     "tool_input": {"command": "private detail"}}
            output = subprocess.run(command, input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
                                    capture_output=True, timeout=10, check=True)
            self.assertEqual(output.stdout, b"")
            self.assertEqual(take_alerts(root), [{"project": "某项目", "tool": "Bash", "thread": "fixture"}])
            self.assertEqual(take_alerts(root), [])
            event["hook_event_name"] = "Stop"
            subprocess.run(command, input=json.dumps(event).encode(), capture_output=True, timeout=10, check=True)
            self.assertEqual(take_alerts(root), [])
