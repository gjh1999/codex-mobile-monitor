import json
import tempfile
import unittest
import uuid
from pathlib import Path

from project_library import (
    ENTRY_PREFIX,
    ProjectLibrary,
    configured_root,
    set_project_root,
)


class ProjectLibraryTests(unittest.TestCase):
    def test_discovers_existing_and_new_projects_and_preserves_session_ids(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        parent, home = base / "projects", base / "codex"
        project = parent / "paint"
        git = project / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        memory = project / "notes" / "Codex"
        memory.mkdir(parents=True)
        (memory / "decision.md").touch()
        (base / "personal-sync.json").write_text(
            json.dumps({"projects": [{"path": str(project), "memoryPath": "notes/Codex"}]}), encoding="utf-8"
        )
        work = project / "src"
        work.mkdir()
        thread = str(uuid.uuid4())
        folder = home / "sessions"
        folder.mkdir(parents=True)
        (folder / f"rollout-{thread}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": str(work)}}) + "\n",
            encoding="utf-8",
        )
        set_project_root(base, parent)
        self.assertEqual(configured_root(base), parent)
        library = ProjectLibrary(base, home)
        rows = {row["id"]: row for row in library.snapshot()}
        self.assertEqual(rows[thread]["cwd"], str(project))
        self.assertEqual(rows[ENTRY_PREFIX + str(project)]["branch"], "main")
        self.assertEqual(rows[ENTRY_PREFIX + str(project)]["notes"], ["decision.md"])
        new = parent / "team" / "new python project"
        new.mkdir(parents=True)
        (new / "pyproject.toml").touch()
        self.assertIn(ENTRY_PREFIX + str(new), {row["id"] for row in library.snapshot()})

    def test_config_can_be_skipped_or_changed(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        library = ProjectLibrary(base, base)
        self.assertEqual(library.snapshot(), [])
        set_project_root(base, None)
        self.assertEqual(library.snapshot(), [])
        set_project_root(base, base / "future")
        self.assertEqual(library.snapshot(), [])
