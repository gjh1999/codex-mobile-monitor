"""Measure an idle poll over already delivered synthetic replies, in this checkout."""
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from daemon_support import WindowsNotifications
from reply_log import ReplyLog

if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        journal = ReplyLog(root / 'replies.db')
        native = WindowsNotifications(root / 'notices.db', lambda *args: 'sent')
        with journal.db:
            journal.db.executemany("INSERT INTO replies VALUES('t','u',?,'agentMessage',?,0)",
                                   ((str(i), 'fixture reply ' * 12) for i in range(2000)))
        while journal.notify_replies(native, 'title') is not None:
            pass
        def previous_poll():
            with journal.lock:
                rows = journal.db.execute("SELECT thread,turn,item,text FROM replies WHERE kind='agentMessage'").fetchall()
            for thread, turn, item, text in rows:
                native.notify(f'reply:{thread}:{turn}:{item}', 'title', ' '.join(text.split())[:160])
        elapsed = {'previous_ms': [], 'fixed_ms': []}
        for _ in range(5):
            for key, call in [('previous_ms', previous_poll), ('fixed_ms', lambda: journal.notify_replies(native, 'title'))]:
                start = time.perf_counter()
                call()
                elapsed[key].append(round((time.perf_counter() - start) * 1000, 3))
        print(json.dumps(dict(rows=2000, rounds=5, sqlite=sqlite3.sqlite_version, **elapsed,
                              median_ms={key: statistics.median(value) for key,value in elapsed.items()})))
        journal.db.close()
        native.db.close()
