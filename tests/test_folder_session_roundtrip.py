"""Two desktops resume a markerless project through the private session channel."""

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from code_sync import value as git
from handoff import session_location
from personal_memory import latest_session
from personal_relay import MAX_RECORD
from personal_sync import PersonalSync
from personal_task_locks import Claim
from project_library import set_project_root


def event(kind, payload):
    return (json.dumps({"timestamp": "2026-11-21T12:00:00Z", "type": kind, "payload": payload})
            + "\n").encode()


def completed_turn(cwd, turn):
    return (event("turn_context", {"cwd": str(cwd)})
            + event("event_msg", {"type": "task_started", "turn_id": turn})
            + event("event_msg", {"type": "task_complete", "turn_id": turn}))


def write_session(home, directory, size):
    tid = str(uuid.uuid4())
    turn = str(uuid.uuid4())
    name = f"rollout-2026-11-21T12-00-00-{tid}.jsonl"
    relative = session_location(name, tid)
    file = home / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    first = (event("session_meta", {"id": tid, "cwd": str(directory)})
             + event("event_msg", {"type": "task_started", "turn_id": turn}))
    last = event("event_msg", {"type": "task_complete", "turn_id": turn})
    empty = event("event_msg", {"type": "agent_message", "message": ""})
    padding = size - len(first + empty + last)
    file.write_bytes(first + event("event_msg", {"type": "agent_message", "message": "a" * padding}) + last)
    return tid, relative


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class PlainProjectRoundtripTests(unittest.TestCase):
    def test_four_original_sessions_cross_homes_and_one_continues(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        share = base / "paired-storage"
        share.mkdir()
        roots = [base / "original-account" / "gjh", base / "new-account" / "Projects"]

        def device(parent):
            data = parent.parent / "lanxi"
            data.mkdir(parents=True)
            parent.mkdir()
            set_project_root(data, parent)
            home = parent.parent / "codex"
            home.mkdir()
            return PersonalSync(data, {"exchange": str(share), "checkoutRoot": str(parent)}, home=home)

        source, receiver = map(device, roots)
        rel = Path("vscodePronject/Satisfactry")
        original, restored = [parent / rel for parent in roots]
        original.mkdir(parents=True)
        secret = b"R38 untracked ordinary project data must stay here"
        (original / "private.txt").write_bytes(secret)
        ref = roots[1] / "AppData/Local/Temp/satisfactory-sbp-source"
        ref.mkdir(parents=True)
        git(None, "init", str(ref))
        (ref / "fixture.txt").write_bytes(b"reference checkout")

        sessions = dict(write_session(source.home, original, round(size * 1024**2))
                        for size in (13.60, 0.51, 17.83, 0.48))
        stop = threading.Event()
        source.cycle(stop)
        receiver.cycle(stop)
        view = receiver.snapshot()
        self.assertEqual(sessions.keys(), view["sessions"].keys(), view["status"])
        self.assertTrue(restored.is_dir())
        for tid, relative in sessions.items():
            with self.subTest(tid=tid):
                self.assertEqual(view["sessions"][tid]["cwd"], str(restored))
                self.assertEqual(Path(view["sessions"][tid]["path"]), receiver.home / relative)
                checksum = file_digest(source.home / relative)
                self.assertEqual(checksum, file_digest(receiver.home / relative))
                # This is the digest the receiving desktop supplies before taking control.
                self.assertEqual(receiver.session_digest(tid), checksum)
                Claim(project=view["sessions"][tid]["project"], checkpoint=checksum)
        self.assertIn(latest_session(restored)[0], sessions)
        self.assertIsNone(latest_session(ref))
        self.assertFalse((restored / "private.txt").exists())
        self.assertFalse((restored / ".git").exists())
        self.assertEqual((ref / "fixture.txt").read_bytes(), b"reference checkout")
        with closing(sqlite3.connect(share / "lanxi-personal.sqlite")) as db:
            biggest, leaked = db.execute(
                "SELECT max(length(data)), max(instr(data,?)) FROM personal_records", (secret,)
            ).fetchone()
            self.assertLess(biggest, MAX_RECORD)
            self.assertEqual(leaked, 0)

        tid, relative = next(iter(sessions.items()))
        with (receiver.home / relative).open("ab") as stream:
            stream.write(completed_turn(restored, str(uuid.uuid4())))
        receiver.cycle(stop)
        source.cycle(stop)
        self.assertEqual(file_digest(source.home / relative), file_digest(receiver.home / relative))
        self.assertEqual(source.snapshot()["sessions"][tid]["cwd"], str(original))
