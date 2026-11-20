"""Advertise the names and sizes of large local project files without reading their contents."""

import json
import socket
from pathlib import Path

from code_sync import LIMIT, auto_path, run
from handoff import local_path
from handoff_sync import atomic_write
from lfs_assets import file_size
from personal_memory import pack
from personal_relay import EMPTY, digest

MAX_FILES = 2048


def registered(root):
    path = root / ".local" / "large-file-list.json"
    return json.loads(path.read_bytes()) if path.is_file() else {}


def register(root, project, directory, paths):
    selections = registered(root)
    chosen = set(selections.get(project, []))
    for path in paths:
        try:
            name = Path(path).relative_to(directory).as_posix()
        except ValueError as error:
            raise ValueError("请选择项目目录内的文件。") from error
        local = local_path(directory, name)
        if not auto_path(name, True, local) or (file_size(local) or 0) < LIMIT:
            raise ValueError(f"请选择项目内至少 {LIMIT // 1024**2} MiB 的普通文件。")
        chosen.add(name)
    if len(chosen) > MAX_FILES:
        raise ValueError("一个项目登记的文件过多。")
    selections[project] = sorted(chosen)
    path = root / ".local" / "large-file-list.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, pack(selections))


def probe(directory, name, memory):
    if name.casefold().startswith(memory.casefold() + "/"):
        return None
    try:
        path = local_path(directory, name)
        if auto_path(name, True, path):
            return file_size(path)
    except (OSError, ValueError):
        pass
    return None


def source_inventory(directory, selected, memory):
    raw = run(directory, "ls-files", "--cached", "-z").stdout
    names = set(raw.decode("utf-8", errors="replace").split("\0")) | set(selected)
    files = {}
    for name in sorted(names - {""}):
        size = probe(directory, name, memory)
        if size is not None and size >= LIMIT:
            files[name] = size
            if len(files) > MAX_FILES:
                raise ValueError("大文件过多，无法生成完整清单。")
    return files


def sync_inventory(root, store, index, projects, stop, *, include_available=False):
    result, errors = {}, {}
    selections = registered(root)
    device = store.client.device
    hostname = socket.gethostname()[:64]
    for project, (directory, memory) in projects.items():
        if stop.is_set():
            break
        try:
            files = source_inventory(directory, selections.get(project, []), memory)
            prefix = f"large-inventory/{project}/"
            own_key = prefix + device
            version = index.get(own_key, EMPTY)
            data = pack({"name": hostname, "files": files})
            if files or version != EMPTY:
                if digest(data) != version:
                    store.put(own_key, version, data)
            missing = {}
            for key, revision in index.items():
                if not key.startswith(prefix) or key == own_key or revision == EMPTY:
                    continue
                row = json.loads(store.read(key))
                owner = str(row["name"])[:64]
                for name, size in row["files"].items():
                    if type(size) is not int or size < LIMIT:
                        continue
                    local = files.get(name)
                    if local is None:
                        local = probe(directory, name, memory)
                    if local != size or include_available:
                        entry = missing.setdefault(name, {"localSize": local, "copies": {}})
                        versions = entry["copies"]
                        versions.setdefault(size, []).append(owner)
                        if include_available:
                            entry.setdefault("devices", {})[key.removeprefix(prefix)] = owner
            if missing:
                result[project] = missing
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors[project] = str(error) if isinstance(error, ValueError) else "大文件清单暂时无法读取"
    return result, errors
