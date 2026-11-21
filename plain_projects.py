"""Map session-bearing ordinary folders between paired project libraries."""

import hashlib
import json
import uuid
from pathlib import Path

from personal_memory import pack, safe_path
from personal_relay import EMPTY, Changed, digest

CATALOG = "folder-projects/"
INDEX = ".lanxi/lanxi-personal-sessions.json"
EXCLUDED = {"appdata", "node_modules", "__pycache__", "windows", "program files",
            "program files (x86)", "programdata", "$recycle.bin", "system volume information"}


def folder_id(relative):
    return ".folder/" + hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()


def folder_relative(parent, directory):
    if not parent or not parent.is_absolute() or not directory.is_relative_to(parent) or directory == parent:
        return None
    relative = directory.relative_to(parent)
    if any(part.startswith(".") or part.casefold() in EXCLUDED for part in relative.parts):
        return None
    try:
        if not parent.is_dir() or parent.resolve() != parent or safe_path(parent, relative.as_posix()) != directory:
            return None
        for base in (directory, *directory.parents):
            if (base / ".git").exists() or (base.exists() and not base.is_dir()):
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


def folder_bindings(parent, home, store, index, cache, git_paths, errors, stop):
    if not parent or not parent.is_dir():
        return {}
    folders = {}
    native = {}
    for cwd in set(native_directories(home, cache)):
        if stop.is_set():
            return {}
        relative = folder_relative(parent, cwd)
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
                    relative = json.loads(payload)["relativePath"]
                    if not isinstance(relative, str) or folder_id(relative) != project:
                        raise ValueError("普通目录身份与相对路径不符")
                    cache["folder-catalog", key] = revision, relative
                else:
                    relative = cached[1]
            else:
                relative = native[project]
            directory = safe_path(parent, relative)
            if folder_relative(parent, directory) != relative:
                raise ValueError("普通目录路径无效或与 Git 工作区重叠")
            if any(directory.is_relative_to(path) or path == directory for path in git_paths):
                raise ValueError("普通目录与已登记 Git 项目重叠")
            if not revision:
                store.put(key, EMPTY, pack({"relativePath": relative}))
            folders[project] = directory
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors[project] = str(error) if isinstance(error, ValueError) else "暂不能核对普通目录映射"
    return folders
