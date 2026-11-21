"""SQLite notebook IDs remain local. Stable UUIDs identify synchronized entries."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from personal_relay import Changed, EMPTY, digest

FIELDS = ("kind", "title", "body", "filed", "updated_at", "manual_group")
PREFIX = "notebook/"
HISTORY_PREFIX = "notebook-history/"


def local_day(stamp):
    instant = datetime.fromisoformat(stamp)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone().date()


def pack(entry):
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def unpack(data):
    kind, title, body, filed, updated, group = json.loads(data)
    if (kind not in ("note", "todo") or not isinstance(title, str) or not isinstance(body, str)
            or type(filed) is not int or filed not in (0, 1)
            or group is not None and (not isinstance(group, str) or len(group) > 80)):
        raise ValueError("随记记录格式无效")
    datetime.fromisoformat(updated)
    return kind, title, body, filed, updated, group


def is_id(value):
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def conflict_copy(entry):
    return (entry[0], "冲突副本", (entry[1] + "\n\n" if entry[1] else "") + entry[2], *entry[3:])


class NotebookStore:
    def __init__(self, path):
        self.path = path
        with self.transaction() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS notebook (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('note','todo')),
                    title TEXT NOT NULL, body TEXT NOT NULL, filed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, manual_group TEXT
                )
            """)
            if "manual_group" not in {column[1] for column in db.execute("PRAGMA table_info(notebook)")}:
                db.execute("ALTER TABLE notebook ADD COLUMN manual_group TEXT")
                groups = path.with_name("lanxi-groups.sqlite3")
                if groups.is_file():
                    with closing(sqlite3.connect(groups)) as legacy:
                        exists = legacy.execute("SELECT 1 FROM sqlite_master WHERE name='groups'").fetchone()
                        if exists:
                            assignments = legacy.execute(
                                "SELECT name,scope,entry FROM groups WHERE scope IN ('note','todo') AND manual=1"
                            ).fetchall()
                            db.executemany("UPDATE notebook SET manual_group=? WHERE kind=? AND id=?", assignments)
            db.execute("""
                CREATE TABLE IF NOT EXISTS notebook_sync (
                    entry INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL, base BLOB
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS notebook_history (
                    key TEXT PRIMARY KEY, uuid TEXT NOT NULL, updated_at TEXT NOT NULL, data BLOB NOT NULL
                )
            """)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def entries(self, kind, archived=False, *, today=None):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""
                SELECT id,title,body,filed,updated_at FROM notebook
                WHERE kind=? AND (kind='todo' OR filed=?)
                  AND (? IS NULL OR ? OR date(updated_at,'localtime') >= ? OR (kind='todo' AND filed=0))
                ORDER BY updated_at DESC,id DESC
            """, (kind, int(archived), today, int(archived), today)).fetchall()

    def daily_dates(self):
        with closing(sqlite3.connect(self.path)) as db:
            dates = db.execute("""
                SELECT date(updated_at,'localtime') AS day FROM notebook
                UNION SELECT date(updated_at,'localtime') AS day FROM notebook_history ORDER BY day DESC
            """)
            return [day for (day,) in dates if day]

    def daily_entries(self, day):
        with closing(sqlite3.connect(self.path)) as db:
            entries = {}
            rows = db.execute("""
                SELECT uuid, data FROM notebook_history
                WHERE date(updated_at,'localtime')=? ORDER BY datetime(updated_at),key
            """, (day,))
            for key, data in rows:
                entries[key] = unpack(data)
            rows = db.execute(f"""
                SELECT coalesce(sync.uuid, notebook.id), {','.join(FIELDS)}
                FROM notebook LEFT JOIN notebook_sync AS sync ON sync.entry=notebook.id
                WHERE date(updated_at,'localtime')=? ORDER BY notebook.id
            """, (day,))
            for key, *entry in rows:
                entries[key] = tuple(entry)
            return sorted(entries.values(), key=lambda entry: entry[4])

    def save(self, kind, entry_id, body, *, expected=None):
        with self.transaction() as db:
            if entry_id is None:
                return db.execute("INSERT INTO notebook(kind,title,body) VALUES(?, '', ?)", (kind, body)).lastrowid
            current = self.row(db, entry_id)
            if current and expected is not None and current[1:3] != expected and body != current[2]:
                updated = db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                return self.insert(db, (kind, "冲突副本", body, current[3], updated, current[5]))
            if current and current[0] == kind and (current[1] or body != current[2]):
                updated = db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                self.remember_previous_day(db, entry_id, current, updated)
                db.execute("UPDATE notebook SET title='',body=?,updated_at=? WHERE id=?", (body, updated, entry_id))
            return entry_id

    def set_filed(self, kind, entry_id, filed):
        with self.transaction() as db:
            current = self.row(db, entry_id)
            if current is None or current[0] != kind or current[3] == int(filed):
                return
            updated = current[4]
            if kind == "todo" or not filed:
                now = db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                if self.remember_previous_day(db, entry_id, current, now):
                    updated = now
            db.execute("UPDATE notebook SET filed=?,updated_at=? WHERE id=?", (int(filed), updated, entry_id))
            return updated

    def remember_previous_day(self, db, entry_id, entry, updated):
        if local_day(entry[4]) == local_day(updated):
            return False
        row = db.execute("SELECT uuid FROM notebook_sync WHERE entry=?", (entry_id,)).fetchone()
        if row is None:
            uid = str(uuid4())
            db.execute("INSERT INTO notebook_sync(entry,uuid) VALUES(?,?)", (entry_id, uid))
        else:
            uid = row[0]
        data = pack(entry)
        self.insert_history(db, uid + "/" + digest(data), entry, data)
        return True

    @staticmethod
    def insert_history(db, key, entry, data):
        return db.execute("INSERT OR IGNORE INTO notebook_history VALUES(?,?,?,?)",
                          (key, key.split("/", 1)[0], entry[4], data)).rowcount > 0

    def history_keys(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {key for (key,) in db.execute("SELECT key FROM notebook_history")}

    def history_data(self, key):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT data FROM notebook_history WHERE key=?", (key,)).fetchone()[0]

    def receive_history(self, key, data):
        entry = unpack(data)
        with self.transaction() as db:
            return self.insert_history(db, key, entry, data)

    def groups(self, kind):
        with closing(sqlite3.connect(self.path)) as db:
            return dict(db.execute("SELECT id,manual_group FROM notebook WHERE kind=? AND manual_group IS NOT NULL",
                                   (kind,)))

    def assign(self, kind, entry_id, name):
        with self.transaction() as db:
            db.execute("UPDATE notebook SET manual_group=? WHERE id=? AND kind=?", (name, entry_id, kind))

    @staticmethod
    def row(db, entry_id):
        return db.execute(f"SELECT {','.join(FIELDS)} FROM notebook WHERE id=?", (entry_id,)).fetchone()

    @staticmethod
    def insert(db, entry):
        return db.execute(f"INSERT INTO notebook({','.join(FIELDS)}) VALUES(?,?,?,?,?,?)", entry).lastrowid

    def snapshot(self):
        with self.transaction() as db:
            ids = db.execute("SELECT id FROM notebook WHERE id NOT IN (SELECT entry FROM notebook_sync)").fetchall()
            db.executemany("INSERT INTO notebook_sync(entry,uuid) VALUES(?,?)",
                           [(row[0], str(uuid4())) for row in ids])
            return {uuid: (entry, base, self.row(db, entry))
                    for entry, uuid, base in db.execute("SELECT entry,uuid,base FROM notebook_sync")}

    def receive(self, key, expected, value, base, conflict=None):
        with self.transaction() as db:
            pair = db.execute("SELECT entry FROM notebook_sync WHERE uuid=?", (key,)).fetchone()
            if pair:
                entry = pair[0]
                if self.row(db, entry) != expected:
                    raise Changed("本机正在编辑，稍后同步")
                if value != expected:
                    self.remember_previous_day(db, entry, expected, value[4])
                    db.execute(f"UPDATE notebook SET {','.join(f'{field}=?' for field in FIELDS)} WHERE id=?",
                               (*value, entry))
            else:
                if expected is not None:
                    raise Changed("本机记录已变化")
                entry = self.insert(db, value)
            db.execute("INSERT OR REPLACE INTO notebook_sync VALUES(?,?,?)", (entry, key, base))
            if conflict is not None:
                self.insert(db, conflict_copy(conflict))
            return value != expected or conflict is not None


def merge(base, local, incoming):
    if local is None or local == incoming or base is not None and local == base:
        return incoming, None
    if base is None:
        return incoming, local
    result = []
    for i, (previous, here, there) in enumerate(zip(base, local, incoming)):
        if i == FIELDS.index("updated_at"):
            result.append(max(here, there))
        elif here == previous or here == there:
            result.append(there)
        elif there == previous:
            result.append(here)
        else:
            return incoming, local
    return tuple(result), None


def sync_notebook(root, store, index, stop):
    book = NotebookStore(root / "lanxi-notebook.sqlite3")
    snapshot = book.snapshot()
    keys = {key.removeprefix(PREFIX) for key in index if key.startswith(PREFIX)} | snapshot.keys()
    changed, errors = False, []
    for key in sorted(keys):
        if stop.is_set():
            break
        if not is_id(key):
            continue
        _, base, local = snapshot.get(key, (None, None, None))
        name = PREFIX + key
        revision = index.get(name, EMPTY)
        try:
            data = pack(local) if local is not None else None
            if revision == digest(data):
                if data is not None and data != base:
                    book.receive(key, local, local, data)
                continue
            if revision == digest(base) or revision == EMPTY:
                if data is not None:
                    store.put(name, revision, data)
                    book.receive(key, local, local, data)
                continue
            remote = store.read(name)
            if digest(remote) != revision:
                raise Changed("云端记录已更新，稍后同步")
            updated, conflict = merge(unpack(base) if base else None, local, unpack(remote))
            merged = pack(updated)
            if merged != remote:
                store.put(name, revision, merged)
            changed |= book.receive(key, local, updated, merged, conflict)
        except (ValueError, OSError, TypeError, sqlite3.Error) as error:
            errors.append(str(error) if isinstance(error, ValueError) else "部分记录暂不可同步")
    history = book.history_keys()
    history_index = {key.removeprefix(HISTORY_PREFIX): value for key, value in index.items()
                     if key.startswith(HISTORY_PREFIX)}
    for key in sorted(history | history_index.keys()):
        if stop.is_set():
            break
        uid, _, checksum = key.partition("/")
        if not is_id(uid) or len(checksum) != 64:
            continue
        name = HISTORY_PREFIX + key
        revision = history_index.get(key, EMPTY)
        try:
            if key in history:
                if revision == EMPTY:
                    store.put(name, EMPTY, book.history_data(key))
            elif revision == checksum:
                data = store.read(name)
                if digest(data) != checksum:
                    raise Changed("云端每日记录已更新，稍后同步")
                changed |= book.receive_history(key, data)
        except (ValueError, OSError, TypeError, sqlite3.Error) as error:
            errors.append(str(error) if isinstance(error, ValueError) else "部分每日记录暂不可同步")
    return changed, errors
