"""Persistent project ownership for device reporters and Codex workers."""
import sqlite3
import time
from contextlib import contextmanager
from secrets import token_urlsafe


class Conflict(ValueError):
    pass


class Ownership:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS revisions (
                    project TEXT PRIMARY KEY, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity (
                    project TEXT, device TEXT, session TEXT, source TEXT NOT NULL,
                    sequence INTEGER NOT NULL, event TEXT NOT NULL, stamp REAL NOT NULL,
                    PRIMARY KEY(project, device, session)
                );
                CREATE TABLE IF NOT EXISTS writes (
                    project TEXT PRIMARY KEY, device TEXT NOT NULL, instance TEXT NOT NULL,
                    thread TEXT NOT NULL, version INTEGER NOT NULL, ticket TEXT NOT NULL
                );
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    @staticmethod
    def version(db, project):
        row = db.execute('SELECT version FROM revisions WHERE project=?', (project,)).fetchone()
        return row['version'] if row else 0

    @staticmethod
    def advance(db, project):
        db.execute('INSERT INTO revisions VALUES (?,1) ON CONFLICT(project) DO UPDATE SET version=version+1',
                   (project,))

    @staticmethod
    def writer(db, project):
        return db.execute('SELECT * FROM writes WHERE project=?', (project,)).fetchone()

    @staticmethod
    def humans(db, project):
        return db.execute("SELECT * FROM activity WHERE project=? AND event!='close' ORDER BY device,session",
                          (project,)).fetchall()

    def state(self, db, project):
        write = self.writer(db, project)
        return dict(version=self.version(db, project),
                    writer=dict(device=write['device'], thread=write['thread']) if write else None,
                    humans=[dict(device=row['device'], session=row['session'], source=row['source'],
                                 stale=self.clock() - row['stamp'] > 30)
                            for row in self.humans(db, project)])

    def status(self, project):
        with self.transaction() as db:
            return self.state(db, project)

    def report(self, project, device, session, source, sequence, event):
        with self.transaction() as db:
            key = (project, device, session)
            row = db.execute('SELECT * FROM activity WHERE project=? AND device=? AND session=?', key).fetchone()
            if row:
                if row['source'] != source or sequence < row['sequence']:
                    raise Conflict('过期或来源不同的活动事件')
                if sequence == row['sequence']:
                    if row['event'] != event:
                        raise Conflict('同一序号不能用于不同事件')
                    return self.state(db, project)
                if row['event'] == 'close':
                    raise Conflict('已关闭的窗口应使用新的会话标识')
            elif event == 'heartbeat':
                raise Conflict('活动会话尚未打开')
            if source == 'cli' and not row and event == 'open':
                if any(h['source'] == 'cli' for h in self.humans(db, project)):
                    raise Conflict('此项目已有 CLI 会话占用，请先核对其状态')
            if (event == 'close' and row) or (event == 'open' and not row):
                self.advance(db, project)
            db.execute("""INSERT INTO activity VALUES (?,?,?,?,?,?,?)
                          ON CONFLICT(project,device,session) DO UPDATE SET
                          sequence=excluded.sequence,event=excluded.event,stamp=excluded.stamp""",
                       (*key, source, sequence, event, self.clock()))
            return self.state(db, project)

    def reserve(self, project, device, instance, thread, version, ticket=None):
        with self.transaction() as db:
            if self.version(db, project) != version or self.humans(db, project):
                raise Conflict('项目控制权已变更或有人正在使用')
            write = self.writer(db, project)
            if write:
                if (write['device'], write['instance'], write['version'], write['ticket']) != (
                        device, instance, version, ticket):
                    raise Conflict('项目有尚未确认结束的后台写入')
                return write['ticket']
            if ticket is not None:
                raise Conflict('此操作的写入凭据已结束')
            self.advance(db, project)
            version = self.version(db, project)
            ticket = token_urlsafe(32)
            db.execute('INSERT INTO writes VALUES (?,?,?,?,?,?)', (project, device, instance, thread, version, ticket))
            return ticket

    def release(self, project, device, ticket):
        with self.transaction() as db:
            write = self.writer(db, project)
            if not write:
                return
            if (write['device'], write['ticket']) != (device, ticket):
                raise Conflict('写入凭据已过期')
            db.execute('DELETE FROM writes WHERE project=?', (project,))
            self.advance(db, project)

    def release_confirmed(self, project, version, device, thread):
        with self.transaction() as db:
            write = self.writer(db, project)
            if (not write or self.version(db, project) != version
                    or (write['device'], write['thread']) != (device, thread)):
                raise Conflict('项目已发生变化，请重新核对')
            db.execute('DELETE FROM writes WHERE project=?', (project,))
            self.advance(db, project)
