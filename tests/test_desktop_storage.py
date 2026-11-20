import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from desktop_storage import PROFILE, prepare_data
from service_lock import ServiceLock


class DesktopStorageTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.old = self.base / "old"
        self.new = self.base / "new" / "release"
        self.target = self.base / "Windows Profile" / "Lanxi" / "data"
        self.old.mkdir()
        self.new.mkdir(parents=True)
        (self.old / "local-config.json").write_text('{"desktopWallpaperMinutes":0}', encoding="utf-8")

    def test_moves_config_and_wal_records_and_keeps_original_workspace(self):
        db_path = self.old / "replies.sqlite3"
        with closing(sqlite3.connect(db_path)) as old:
            old.execute("PRAGMA journal_mode=WAL")
            old.execute("CREATE TABLE fixture (text TEXT)")
            old.execute("INSERT INTO fixture VALUES ('old reply')")
            old.commit()
            self.assertTrue(db_path.with_suffix(".sqlite3-wal").is_file())
            cache = self.old / ".local" / "personal-sync" / "home"
            cache.mkdir(parents=True)
            (cache / "catalog.json").write_text("{}", encoding="utf-8")
            (self.old / "not-a-setting.bin").touch()
            workspace = prepare_data(self.new, self.target, lambda _: self.old)
            self.assertEqual(workspace, self.old.parent)
            self.assertEqual(
                (self.target / "local-config.json").read_bytes(),
                (self.old / "local-config.json").read_bytes(),
            )
            self.assertTrue((self.target / cache.relative_to(self.old) / "catalog.json").exists())
            self.assertFalse((self.target / "not-a-setting.bin").exists())
            with closing(sqlite3.connect(self.target / db_path.name)) as copied:
                self.assertEqual(copied.execute("SELECT text FROM fixture").fetchone(), ("old reply",))
            self.assertEqual(old.execute("SELECT text FROM fixture").fetchone(), ("old reply",))

    def test_uses_fixed_profile_on_later_runs_from_other_folders(self):
        moved = self.base / "different release" / "desktop"
        moved.mkdir(parents=True)
        prepare_data(self.old, self.target, lambda _: self.fail("Expected automatic import"))
        (self.target / "local-config.json").write_text('{"new":true}', encoding="utf-8")
        (moved / "local-config.json").write_text('{"new":false}', encoding="utf-8")
        self.assertEqual(
            prepare_data(moved, self.target, lambda _: self.fail("Asked again")), self.old.parent
        )
        self.assertEqual((self.target / "local-config.json").read_bytes(), b'{"new":true}')

    def test_waits_for_old_version_and_retries_failed_copy(self):
        with closing(ServiceLock(self.old / "monitor.lockfile")), self.assertRaises(RuntimeError):
            prepare_data(self.old, self.target, lambda _: False)
        self.assertFalse(self.target.exists())
        broken = self.old / "replies.sqlite3"
        broken.write_bytes(b"invalid database")
        with self.assertRaises(sqlite3.DatabaseError):
            prepare_data(self.new, self.target, lambda _: self.old)
        self.assertFalse(self.target.exists())
        self.assertEqual(broken.read_bytes(), b"invalid database")
        broken.unlink()
        prepare_data(self.old, self.target, lambda _: False)
        self.assertTrue((self.target / PROFILE).exists())

    def test_cancel_and_fresh_install(self):
        self.assertIsNone(prepare_data(self.new, self.target, lambda _: None))
        self.assertFalse(self.target.exists())
        self.assertEqual(prepare_data(self.new, self.target, lambda _: False), self.new.parent)
        self.assertIsNone(json.loads((self.target / PROFILE).read_text(encoding="utf-8"))["importedFrom"])
