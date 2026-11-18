"""Relay local IDE project lifecycles to the configured coordinator."""
import argparse
import json
import queue
import sqlite3
import sys
import threading
import uuid
from contextlib import closing
from pathlib import Path

from coordination_client import ControlDenied, device_client


OUTBOX_SCHEMA = """CREATE TABLE IF NOT EXISTS pending_close (
    url TEXT, device TEXT, project TEXT, session TEXT, source TEXT, sequence INTEGER,
    PRIMARY KEY (url, device, project, session)
)"""


def close_queue(root):
    return root / '.local' / 'ide-close.sqlite3'


def enqueue_close(path, client, project, session, source, sequence):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(OUTBOX_SCHEMA)
        db.execute("INSERT OR REPLACE INTO pending_close VALUES (?,?,?,?,?,?)",
                   (client.url, client.device, project, session, source, sequence))


def flush_closes(path, client):
    if not path.is_file():
        return
    with closing(sqlite3.connect(path, timeout=1)) as db:
        projects = list(client.paths.values())
        if not projects:
            return
        db.execute(OUTBOX_SCHEMA)
        placeholders = ','.join('?' for _ in projects)
        rows = db.execute(f"""SELECT project,session,source,sequence FROM pending_close
                              WHERE url=? AND device=? AND project IN ({placeholders}) ORDER BY rowid LIMIT 8""",
                          (client.url, client.device, *projects)).fetchall()
        for project, session, source, sequence in rows:
            try:
                client.event(project, session, source, sequence, 'close')
            except ControlDenied:
                return
            with db:
                db.execute("""DELETE FROM pending_close
                              WHERE url=? AND device=? AND project=? AND session=? AND sequence=?""",
                           (client.url, client.device, project, session, sequence))


def report(client, outbox, project, source, commands, heartbeat=5):
    session = str(uuid.uuid4())
    sequence = 1
    opened = False
    replayed = False
    reported_error = False
    while True:
        try:
            client.event(project, session, source, sequence, 'heartbeat' if opened else 'open')
        except ControlDenied as error:
            if not reported_error and error.detail:
                print(error.detail, file=sys.stderr, flush=True)
                reported_error = True
        else:
            opened = True
            reported_error = False
            sequence += 1
            if not replayed:
                try:
                    flush_closes(outbox, client)
                except sqlite3.OperationalError:
                    pass
                else:
                    replayed = True
        try:
            command = commands.get(timeout=heartbeat)
        except queue.Empty:
            continue
        if command == 'close':
            # A lost open/heartbeat reply may still have committed at the
            # center. Use a later sequence to close either outcome.
            enqueue_close(outbox, client, project, session, source, sequence + 1)
            flush_closes(outbox, client)
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=('vscode', 'pycharm'), required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('local-config.json'))
    args = parser.parse_args()
    try:
        if not args.config.is_file():
            return
        client = device_client(json.loads(args.config.read_text(encoding='utf-8-sig')))
        project = client.project_at(args.root) if client else None
        if project is None:
            return
        if not client.token:
            raise ValueError('设备尚未配置 LANXI_DEVICE_TOKEN')
        commands = queue.Queue(maxsize=1)

        def read_close():
            commands.put(sys.stdin.readline().strip())

        threading.Thread(target=read_close, daemon=True).start()
        outbox = close_queue(args.config.resolve().parent)
        # Establish occupancy before retrying any previous window's close.
        report(client, outbox, project, args.source, commands)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        parser.exit(1, f'IDE 活动上报失败：{error}\n')


if __name__ == '__main__':
    main()
