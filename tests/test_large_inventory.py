import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from code_sync import LIMIT, run
from large_inventory import register, sync_inventory
from personal_relay import Records


class InventoryTests(unittest.TestCase):
    def test_reports_absent_and_different_sizes_without_sending_file_bytes(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            relay = Records(root / "relay.sqlite3", "personal")
            project = "owner/example"
            a, b = root / "desk", root / "laptop"
            for repo in (a, b):
                run(None, "init", str(repo))
            asset = a / "shared.dat"
            asset.write_bytes(b"private test bytes")
            with asset.open("ab") as stream:
                stream.truncate(LIMIT + 1)
            run(a, "add", "shared.dat")
            extra = a / "model.weights"
            with extra.open("wb") as stream:
                stream.truncate(LIMIT)
            register(root, project, a, [extra])
            with patch("large_inventory.socket.gethostname", return_value="desktop"):
                relay.client = SimpleNamespace(device="desk")
                value, errors = sync_inventory(root, relay, relay.index(), {project: (a, ".codex")}, threading.Event())
            self.assertEqual(value, {})
            self.assertEqual(errors, {})
            relay.client = SimpleNamespace(device="laptop")
            (b / "model.weights").write_bytes(b"short and different")
            files, errors = sync_inventory(root, relay, relay.index(), {project: (b, ".codex")}, threading.Event())
            self.assertEqual(errors, {})
            self.assertEqual(files[project]["shared.dat"], {"localSize": None, "copies": {LIMIT + 1: ["desktop"]}})
            self.assertEqual(files[project]["model.weights"]["localSize"], len(b"short and different"))
            self.assertFalse((b / "shared.dat").exists())
            entry = relay.read(f"large-inventory/{project}/desk")
            self.assertLess(len(entry), 256)
            self.assertEqual(json.loads(entry)["files"], {"model.weights": LIMIT, "shared.dat": LIMIT + 1})
            self.assertNotIn(b"private test bytes", entry)
            asset.unlink()
            relay.client = SimpleNamespace(device="desk")
            sync_inventory(root, relay, relay.index(), {project: (a, ".codex")}, threading.Event())
            relay.client = SimpleNamespace(device="laptop")
            files, _ = sync_inventory(root, relay, relay.index(), {project: (b, ".codex")}, threading.Event())
            self.assertNotIn("shared.dat", files[project])
