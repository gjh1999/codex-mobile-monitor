import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from notebook_store import NotebookStore, sync_notebook
from personal_relay import Records


class NotebookSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.a, self.b = self.root / "A", self.root / "B"
        self.a.mkdir()
        self.b.mkdir()
        self.stop = threading.Event()
        self.relay = Records(self.root / "relay.sqlite3", "private-test-space")

    def sync(self, device):
        changed, errors = sync_notebook(device, self.relay, self.relay.index(), self.stop)
        self.assertEqual(errors, [])
        return changed

    def test_notes_archive_manual_groups_done_state_and_conflict_copies(self):
        old_path = self.a / "lanxi-notebook.sqlite3"
        with closing(sqlite3.connect(old_path)) as db, db:
            db.execute("""CREATE TABLE notebook (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                        body TEXT NOT NULL, filed INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("INSERT INTO notebook(kind,title,body,filed) VALUES('note','','旧版随记',1)")
        with closing(sqlite3.connect(self.a / "lanxi-groups.sqlite3")) as db, db:
            db.execute("CREATE TABLE groups(scope,entry,digest,name,manual)")
            db.execute("INSERT INTO groups VALUES('note','1','','重要',1)")
        a = NotebookStore(old_path)
        todo = a.save("todo", None, "待办初稿")
        self.sync(self.a)
        self.assertTrue(self.sync(self.b))
        b = NotebookStore(self.b / old_path.name)
        note_id = b.entries("note", archived=True)[0][0]
        self.assertEqual(b.groups("note"), {note_id: "重要"})
        self.assertEqual(b.entries("note", archived=True)[0][2], "旧版随记")
        other_id = b.entries("todo")[0][0]
        a.save("todo", todo, "台式机改了正文")
        b.set_filed("todo", other_id, True)
        self.sync(self.a)
        self.assertTrue(self.sync(self.b))
        self.sync(self.a)
        self.assertEqual(a.entries("todo")[0][2:4], ("台式机改了正文", 1))
        a.save("todo", todo, "台式机离线版本")
        b.save("todo", other_id, "笔记本离线版本")
        self.sync(self.a)
        self.assertTrue(self.sync(self.b))
        self.sync(self.b)
        self.sync(self.a)
        for notebook in (a, b):
            entries = notebook.entries("todo")
            self.assertEqual({entry[2] for entry in entries}, {"台式机离线版本", "笔记本离线版本"})
            self.assertEqual(sum(entry[1] == "冲突副本" for entry in entries), 1)
        local_saved = b.save("todo", other_id, "编辑区草稿", expected=("", "较旧正文"))
        self.assertNotEqual(local_saved, other_id)
        self.assertIn("编辑区草稿", {row[2] for row in b.entries("todo")})
        self.assertTrue(all(isinstance(json.loads(self.relay.read(key)), list) for key in self.relay.index()))

    def test_daily_record_retains_previous_text_and_todo_state_after_sync(self):
        book = NotebookStore(self.a / "lanxi-notebook.sqlite3")
        today = date.today()
        yesterday = today - timedelta(days=1)
        before = datetime.combine(yesterday, time(12)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with book.transaction() as db:
            note = book.insert(db, ("note", "旧题", "昨天的笔记", 0, before, None))
            todo = book.insert(db, ("todo", "", "跨天待办", 0, before, None))
            book.insert(db, ("todo", "", "昨天就完成", 1, before, None))

        self.assertEqual(book.daily_dates(), [yesterday.isoformat()])
        self.assertFalse(book.entries("note", today=today.isoformat()))
        self.assertEqual([row[0] for row in book.entries("todo", today=today.isoformat())], [todo])
        book.save("note", note, "今天的笔记")
        book.set_filed("todo", todo, True)
        self.assertEqual(book.daily_dates(), [today.isoformat(), yesterday.isoformat()])
        self.assertEqual({entry[1:4] for entry in book.daily_entries(yesterday.isoformat())}, {
            ("旧题", "昨天的笔记", 0), ("", "跨天待办", 0), ("", "昨天就完成", 1),
        })
        self.assertEqual({entry[2:4] for entry in book.daily_entries(today.isoformat())}, {
            ("今天的笔记", 0), ("跨天待办", 1),
        })
        self.sync(self.a)
        self.assertTrue(self.sync(self.b))
        other = NotebookStore(self.b / book.path.name)
        self.assertEqual(other.daily_dates(), book.daily_dates())
        for day in book.daily_dates():
            self.assertCountEqual(other.daily_entries(day), book.daily_entries(day))
        self.assertFalse(self.sync(self.b))
        self.assertEqual(sum(key.startswith("notebook-history/") for key in self.relay.index()), 2)
