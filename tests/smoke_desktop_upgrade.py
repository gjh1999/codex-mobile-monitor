"""Use two releases with one isolated Windows data directory."""

import argparse
import json
import shutil
import tempfile
import time
from contextlib import closing
from pathlib import Path

from desktop_notebook import NotebookStore
from desktop_storage import PROFILE
from reply_log import ReplyLog
from tests.smoke_personal_exe import running, wait_for, write_json


def verify(old_exe, new_exe, root):
    started = time.monotonic()
    old = root / "old R20"
    new = root / "new R21"
    moved = root / "moved R21"
    appdata = root / "Windows account"
    for folder, executable in ((old, old_exe), (new, new_exe), (moved, new_exe)):
        folder.mkdir()
        shutil.copy2(executable, folder / executable.name)

    path = old / "local-config.json"
    original = {"desktopWallpaperMinutes": 0}
    pair = {
        "codeOnly": True,
        "checkoutRoot": str(root / "my projects"),
        "registry": str(root / "registry.json"),
    }
    write_json(path, original)
    write_json(old / "personal-sync.json", pair)
    with running(old_exe, old, local_appdata=appdata) as proc:
        wait_for("启动旧版应用", (old / "task-control.sqlite3").is_file, proc, 15)

    saved = NotebookStore(old / "lanxi-notebook.sqlite3")
    saved.save("note", None, "legacy note")
    todo = saved.save("todo", None, "legacy completed todo")
    saved.set_filed("todo", todo, True)
    with closing(ReplyLog(old / "replies.sqlite3").db) as journal, journal:
        journal.execute(
            "INSERT INTO replies VALUES (?,?,?,?,?,?)",
            ("old-thread", "turn", "item", "agentMessage", "legacy reply", time.time()),
        )

    target = appdata / "Lanxi" / "data"
    with running(new_exe, new, local_appdata=appdata, arguments=("--import-data-from", str(old))) as proc:
        wait_for("新版本迁移旧数据", (target / PROFILE).is_file, proc, 20)
        assert json.loads((target / PROFILE).read_bytes())["importedFrom"] == str(old)
        assert json.loads((target / "personal-sync.json").read_bytes()) == pair
        assert json.loads((target / "local-config.json").read_bytes()) == original
        notes = NotebookStore(target / "lanxi-notebook.sqlite3")
        assert notes.entries("note")[0][2] == "legacy note"
        assert notes.entries("todo")[0][2:4] == ("legacy completed todo", 1)
        with closing(ReplyLog(target / "replies.sqlite3").db) as journal:
            assert journal.execute("SELECT text FROM replies WHERE thread='old-thread'").fetchone() == (
                "legacy reply",
            )
        write_json(target / "local-config.json", {"desktopWallpaperMinutes": 5})
        notes.save("note", None, "new note")

    write_json(moved / "local-config.json", {"desktopWallpaperMinutes": 99})
    (target / "monitor.lockfile").unlink(missing_ok=True)
    with running(new_exe, moved, local_appdata=appdata) as proc:
        wait_for("从不同目录打开新版", (target / "monitor.lockfile").is_file, proc, 15)
        # Wait for the new desktop to reach the background path after parsing its preferences.
        wait_for("新版使用固定设置", lambda: len(notes.entries("note")) == 2, proc, 10)
        assert json.loads((target / "local-config.json").read_bytes()) == {"desktopWallpaperMinutes": 5}
        assert json.loads(path.read_bytes()) == original
        assert len(saved.entries("note")) == 1
        assert not (moved / "replies.sqlite3").exists()

    return {
        "versions": [old_exe.name, new_exe.name],
        "profile_is_shared_across_install_locations": True,
        "legacy_configuration_intact": True,
        "pairing_and_project_path": True,
        "notebook_todo_and_replies": True,
        "seconds": round(time.monotonic() - started, 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lanxi-upgrade-") as scratch:
        result = verify(args.old.resolve(), args.new.resolve(), Path(scratch))
    write_json(args.output, result)
    print(json.dumps(result))
