"""Durable, deduplicated message journal. Never persist arbitrary RPC payloads."""
import json
import re
import sqlite3
import threading
import time


class ReplyLog:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.active_turns = set()
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('CREATE TABLE IF NOT EXISTS replies(thread TEXT, turn TEXT, item TEXT, kind TEXT, text TEXT, received REAL, PRIMARY KEY(thread,turn,item))')
            self.db.execute('CREATE TABLE IF NOT EXISTS task_events(thread TEXT, turn TEXT, method TEXT, status TEXT, received REAL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS replies_recent ON replies(thread,received)')
            self.db.execute('CREATE INDEX IF NOT EXISTS task_events_recent ON task_events(thread,received)')
            self.db.execute("CREATE INDEX IF NOT EXISTS completed_turns ON task_events(thread,turn) WHERE method='turn/completed'")
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_reply_notifications'").fetchone():
                self.db.execute('CREATE TABLE pending_reply_notifications(thread TEXT, turn TEXT, item TEXT, legacy INTEGER DEFAULT 0, PRIMARY KEY(thread,turn,item))')
                self.db.execute('CREATE INDEX notification_order ON pending_reply_notifications(legacy)')
                self.db.execute("""INSERT INTO pending_reply_notifications SELECT thread,turn,item,1 FROM replies
                                   WHERE kind='agentMessage' ORDER BY rowid""")
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS queue_agent_reply AFTER INSERT ON replies
                               WHEN NEW.kind='agentMessage' BEGIN
                               INSERT OR IGNORE INTO pending_reply_notifications(thread,turn,item) VALUES(NEW.thread,NEW.turn,NEW.item);
                               END""")

    def track_turn(self, thread, turn):
        """A start acknowledgement or an active-turn read can arrive after completion."""
        if turn:
            with self.lock:
                completed = self.db.execute("SELECT 1 FROM task_events WHERE thread=? AND turn=? AND method='turn/completed' LIMIT 1",
                                            (thread, turn)).fetchone()
                if not completed:
                    self.active_turns.add((thread, turn))

    def record(self, event, owned):
        method = event.get('method')
        if method not in ('turn/started', 'turn/completed', 'thread/status/changed', 'thread/goal/updated', 'error', 'item/started', 'item/completed'):
            return False
        params = event.get('params') or {}
        thread = params.get('threadId')
        turn = params.get('turnId') or (params.get('turn') or {}).get('id')
        key = thread, turn
        with self.lock, self.db:
            if thread not in owned and key not in self.active_turns:
                return False
            if turn and method in ('turn/started', 'item/started'):
                self.active_turns.add(key)
            if method == 'item/started':
                return False
            if method == 'item/completed':
                item = params.get('item') or {}
                kind = item.get('type')
                if kind not in ('agentMessage', 'userMessage') or not item.get('id') or not turn:
                    return False
                body = item.get('text') if kind == 'agentMessage' else json.dumps(item.get('content', []), ensure_ascii=False)
                if not isinstance(body, str):
                    return False
                inserted = self.db.execute('INSERT OR IGNORE INTO replies VALUES(?,?,?,?,?,?)',
                                           (thread, turn, item['id'], kind, body, time.time()))
                return bool(inserted.rowcount)
            status = params.get('status', (params.get('turn') or {}).get('status', (params.get('goal') or {}).get('status')))
            error = params.get('error') or (params.get('turn') or {}).get('error')
            if error:
                if isinstance(error, dict):
                    error = {field: error.get(field) for field in ('message', 'code', 'codexErrorInfo')}
                safe = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', json.dumps(error, ensure_ascii=False))[:1500]
                status = {'status': status, 'error': safe}
            self.db.execute('INSERT INTO task_events VALUES(?,?,?,?,?)', (thread, turn, method, json.dumps(status), time.time()))
            if method == 'turn/completed':
                self.active_turns.discard(key)
        return False

    def notify_replies(self, native, title, suffix='', limit=64):
        with self.lock:
            rows = self.db.execute("""SELECT pending.thread,pending.turn,pending.item,replies.text
                                      FROM pending_reply_notifications AS pending
                                      CROSS JOIN replies USING(thread,turn,item)
                                      ORDER BY pending.legacy,pending.rowid LIMIT ?""", (limit,)).fetchall()
        result = None
        for thread, turn, item, text in rows:
            excerpt = ' '.join(text.split())[:160]
            result = native.notify(f'reply:{thread}:{turn}:{item}', title, excerpt + suffix)
        if rows:
            # Existing delivery keys make replay safe after interruption, including migration.
            with self.lock, self.db:
                self.db.executemany('DELETE FROM pending_reply_notifications WHERE thread=? AND turn=? AND item=?',
                                    [row[:3] for row in rows])
        return result
