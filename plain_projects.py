"""Map session-bearing ordinary folders between paired project libraries."""

import hashlib
import json
import uuid
from pathlib import Path

from personal_memory import pack, safe_path
from personal_relay import EMPTY, Changed, digest

CATALOG = "folder-projects/"
INDEX = ".lanxi/lanxi-personal-sessions.json"
RECEIVER = ".lanxi/receiver.json"
EXCLUDED = {"appdata", "node_modules", "__pycache__", "windows", "program files",
            "program files (x86)", "programdata", "$recycle.bin", "system volume information",
            "temp", "tmp", "cache", "caches", "build-exe", "site-packages", "venv"}


def folder_id(relative):
    return ".folder/" + hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()


def receiver_placeholder(directory, expected):
    if not directory.is_dir() or {item.name for item in directory.iterdir()} != {".lanxi"}:
        return False
    try:
        marker = safe_path(directory, RECEIVER)
        return marker.is_file() and json.loads(marker.read_bytes()) == {"folder": expected}
    except (OSError, ValueError):
        return False


def folder_relative(parent, directory, *, allow_git=False):
    if not parent or not parent.is_absolute() or not directory.is_relative_to(parent) or directory == parent:
        return None
    relative = directory.relative_to(parent)
    if any(part.startswith(".") or part.casefold() in EXCLUDED for part in relative.parts):
        return None
    try:
        if not parent.is_dir() or parent.resolve() != parent or safe_path(parent, relative.as_posix()) != directory:
            return None
        for base in (directory, *directory.parents):
            if ((base / ".git").exists() and not (allow_git and base == directory)
                    or (base.exists() and not base.is_dir())):
                return None
            if base == parent:
                break
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def native_directories(home, cache):
    for path in (home / "sessions").rglob("rollout-*.jsonl"):
        try:
            cached = cache.get(("folder-cwd", path))
            stat = path.stat()
            stamp = stat.st_ino, stat.st_mtime_ns, stat.st_size
            if cached is None or cached[0] != stamp:
                safe_path(home, path.relative_to(home).as_posix())
                with path.open("rb") as stream:
                    header = json.loads(stream.readline(262144))
                sid = header["payload"]["id"]
                if str(uuid.UUID(sid)) != path.stem[-36:] or header["type"] != "session_meta":
                    continue
                cached = stamp, Path(header["payload"]["cwd"])
                cache["folder-cwd", path] = cached
            yield cached[1]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def folder_bindings(parent, home, store, index, cache, git_projects, code_folders, device, errors, stop):
    if not parent or not parent.is_dir():
        return {}, {}
    folders = {}
    sources = {}
    native = {}
    by_root = {path: project for project, path in git_projects.items()}
    for cwd in set(native_directories(home, cache)) | set(code_folders):
        if stop.is_set():
            break
        if not folder_relative(parent, cwd, allow_git=cwd in code_folders):
            continue
        cwd = next((p for p in (cwd, *cwd.parents) if p in code_folders), cwd)
        relative = folder_relative(parent, cwd, allow_git=cwd in code_folders)
        if relative and cwd.is_dir():
            native[folder_id(relative)] = relative
    revisions = {".folder/" + key[len(CATALOG):]: rev for key, rev in index.items()
                 if key.startswith(CATALOG) and rev != EMPTY}
    for project in native.keys() | revisions.keys():
        if stop.is_set():
            break
        key = CATALOG + project.split("/", 1)[1]
        revision = revisions.get(project)
        try:
            if revision:
                cached = cache.get(("folder-catalog", key))
                if cached is None or cached[0] != revision:
                    payload = store.read(key)
                    if digest(payload) != revision:
                        raise Changed("普通目录登记刚刚更新")
                    record = json.loads(payload)
                    relative = record["relativePath"]
                    if not isinstance(relative, str) or folder_id(relative) != project:
                        raise ValueError("普通目录身份与相对路径不符")
                    cache["folder-catalog", key] = revision, record
                else:
                    record = cached[1]
                    relative = record["relativePath"]
            else:
                relative = native[project]
                record = {"relativePath": relative, "originDevice": device}
            directory = safe_path(parent, relative)
            owns = record.get("originDevice") == device
            linked = record.get("githubProject")
            git_project = by_root.get(directory)
            if linked and git_project and git_project != linked:
                raise ValueError("普通项目登记的 GitHub 仓库与本机不一致")
            if owns and directory in code_folders and git_project and not linked:
                linked = git_project
                record = {**record, "githubProject": linked}
                store.put(key, revision or EMPTY, pack(record))
                revision = digest(pack(record))
            # Git initialization may succeed before origin is available. Keep serving
            # the folder sessions while the owning device retries publication.
            allow_git = owns and directory in code_folders or bool(linked and linked == git_project)
            if folder_relative(parent, directory, allow_git=allow_git) != relative:
                raise ValueError("普通目录路径无效或与 Git 工作区重叠")
            if any(directory.is_relative_to(path) for path in by_root if path != directory or not allow_git):
                raise ValueError("普通目录与已登记 Git 项目重叠")
            if not revision:
                store.put(key, EMPTY, pack(record))
            if owns and project in native and not safe_path(directory, RECEIVER).exists():
                sources[project] = directory
            folders[project] = directory
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            errors[project] = str(error) if isinstance(error, ValueError) else "暂不能核对普通目录映射"
    return folders, sources
