"""Discover local projects and connect their saved Codex conversations to the desktop."""

import json
import stat
import uuid
from pathlib import Path

from handoff_sync import atomic_write
from history_labels import read_label
from personal_memory import NOTE_TYPES, safe_path

SETTINGS = "project-library.json"
ENTRY_PREFIX = "project:"
MARKERS = {
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
    "pom.xml",
    "README.md",
    "AGENTS.md",
    "requirements.txt",
}


def configured_root(data):
    try:
        folder = json.loads((data / SETTINGS).read_bytes())["root"]
        if folder:
            path = Path(folder)
            if path.is_absolute():
                return path
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def set_project_root(data, folder):
    payload = json.dumps({"root": str(folder.resolve()) if folder else None}, ensure_ascii=False).encode()
    atomic_write(data / SETTINGS, payload)


def folders(parent):
    try:
        return sorted(
            child
            for child in parent.iterdir()
            if not child.name.startswith(".")
            and child.name not in ("node_modules", "__pycache__", "build-exe")
            and child.is_dir()
            and not child.is_symlink()
            and not getattr(child.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    except OSError:
        return []


def inspect_project(path, overrides):
    try:
        filenames = {p.name for p in path.iterdir()}
    except OSError:
        return None
    if not (
        filenames.intersection(MARKERS)
        or any(name.endswith((".sln", ".slnx", ".code-workspace")) for name in filenames)
        or (path / ".codex" / "memories").is_dir()
    ):
        return None
    branch = ""
    try:
        with (path / ".git" / "HEAD").open(encoding="utf-8") as stream:
            branch = stream.readline(1024).strip().removeprefix("ref: refs/heads/")
    except (OSError, UnicodeError):
        pass
    memory = ""
    notes = []
    try:
        directory = safe_path(path, overrides.get(path, ".codex/memories"))
        if directory.is_dir():
            memory = str(directory)
            notes = sorted(
                file.name
                for file in directory.iterdir()
                if not file.name.startswith(".")
                and file.suffix.lower() in NOTE_TYPES
                and file.is_file()
                and not file.is_symlink()
            )[:10]
    except (OSError, ValueError, TypeError):
        pass
    return {
        "id": ENTRY_PREFIX + str(path),
        "cwd": str(path),
        "title": path.name,
        "memory": memory,
        "notes": notes,
        "branch": branch,
    }


def discover(parent, overrides):
    projects = {}

    def record(path, depth):
        project = inspect_project(path, overrides)
        if project:
            projects[path] = project
        elif depth:
            for child in folders(path):
                record(child, depth - 1)

    if (parent / ".git").exists():
        record(parent, 0)
    else:
        for child in folders(parent):
            record(child, 1)
        if not projects:
            record(parent, 0)
    return projects


class ProjectLibrary:
    def __init__(self, data, home):
        self.data = data
        self.home = home
        self.headers = {}

    def snapshot(self):
        root = configured_root(self.data)
        if not root:
            return []
        overrides = {}
        try:
            entries = json.loads((self.data / "personal-sync.json").read_bytes()).get("projects", [])
            overrides = {
                Path(row["path"]): row["memoryPath"]
                for row in entries
                if row.get("path") and row.get("memoryPath")
            }
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        projects = discover(root, overrides)
        if not projects:
            return []
        rows = []
        cache = {}
        for file in (self.home / "sessions").rglob("rollout-*.jsonl"):
            try:
                info = file.stat()
                stamp = info.st_mtime_ns, info.st_size
            except OSError:
                continue
            old = self.headers.get(file)
            label = old[1] if old and old[0] == stamp else read_label(file)
            cache[file] = stamp, label
            if not label:
                continue
            thread, fields = label
            try:
                if str(uuid.UUID(thread)) != thread:
                    continue
            except ValueError:
                continue
            if not fields["cwd"]:
                continue
            path = Path(fields["cwd"])
            parent = next((part for part in (path, *path.parents) if part in projects), None)
            if parent:
                rows.append({"id": thread, **fields, "cwd": str(parent)})
        self.headers = cache
        return [*projects.values(), *rows]

    def run(self, stop, publish):
        last = None
        while not stop.is_set():
            try:
                items = self.snapshot()
                if items != last:
                    publish(indexedProjects=items)
                    last = items
            except (OSError, ValueError):
                pass
            stop.wait(5)
