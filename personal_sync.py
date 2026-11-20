"""One personal sync worker per workstation. GitHub carries code; paired storage carries private state."""

import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import threading
from pathlib import Path

from code_sync import (
    SyncIssue,
    exclude_memory,
    github_id,
    sync_project,
    value,
    verify_checkout,
)
from coordination_client import ControlClient
from handoff_sync import atomic_write
from personal_memory import Mirror, pack, safe_path, sync_memories
from personal_relay import EMPTY, Records, RemoteRecords, digest
from project_library import configured_root, discover


class PersonalSync:
    def __init__(self, root, settings, coordination=None, home=None):
        self.root = Path(root)
        self.settings = settings
        self.home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.registry = Path(settings.get("registry", self.home / "git-sync-projects.json"))
        self.checkout = Path(settings.get("checkoutRoot", Path.home() / "LanxiProjects"))
        if not self.checkout.is_absolute():
            raise ValueError("本机项目根目录需要绝对路径")
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.projects = {}
        self.threads = {}
        self.status = {}
        self.completed = {}
        self.epoch = 0
        self.coordination = coordination
        self.priority = None
        self.poll_cache = {}
        self.local_state = self.root / ".local" / "personal-sync"
        self.scope = None

    def store(self):
        if self.settings.get("codeOnly"):
            return None
        if self.settings.get("exchange"):
            share = Path(self.settings["exchange"])
            if not share.is_absolute() or not share.is_dir():
                raise SyncIssue("私人共享目录不可用")
            if any((p / ".git").exists() for p in (share, *share.parents)):
                raise SyncIssue("私人共享目录必须位于 Git 项目之外")
            return Records(share / "lanxi-personal.sqlite", "personal")
        if self.coordination:
            client = ControlClient(self.coordination, os.environ.get("LANXI_DEVICE_TOKEN"))
            return RemoteRecords(client)
        return None

    def snapshot(self):
        with self.lock:
            return {
                "projects": dict(self.projects),
                "sessions": dict(self.threads),
                "status": dict(self.status),
                "completed": dict(self.completed),
                "epoch": self.epoch,
            }

    def request(self, project):
        with self.lock:
            self.priority = project
            epoch = self.completed.get(project, 0)
        self.wake.set()
        return epoch

    def resolve(self, tid, directory=None, remote=None):
        with self.lock:
            thread = self.threads.get(tid, {})
            project = thread.get("project") or remote
            if project not in self.projects and directory:
                current = Path(directory)
                candidates = [
                    (len(Path(path).parts), p)
                    for p, path in self.projects.items()
                    if current.is_relative_to(path)
                ]
                project = max(candidates)[1] if candidates else None
            if project in self.projects:
                return project, Path(self.projects[project])
        return None

    def bindings(self, store, index, cached, published, errors):
        local = {}
        parent = configured_root(self.root)
        found = {}
        if parent and parent.is_dir():
            for path in discover(parent, {}):
                if not (path / ".git").exists():
                    continue
                try:
                    remote = value(path, "config", "--get", "remote.origin.url")
                except (OSError, SyncIssue, subprocess.TimeoutExpired):
                    continue
                identity = github_id(remote)
                if identity:
                    found.setdefault(identity, []).append({"path": str(path), "remote": remote})
        discovered = [rows[0] for rows in found.values() if len(rows) == 1]
        entries = (
            json.loads(self.registry.read_text(encoding="utf-8-sig")).get("projects", [])
            if self.registry.is_file()
            else []
        )
        entries = [*discovered, *entries, *self.settings.get("projects", [])]
        remote_specs = {} if store else cached.copy()
        if store:
            for key, revision in index.items():
                project = key.removeprefix("repo/")
                if project == key or revision == EMPTY:
                    continue
                record = cached.get(project)
                if record is None or digest(pack(record)) != revision:
                    data = store.read(key)
                    if digest(data) != revision:
                        raise SyncIssue("项目登记正在更新")
                    record = json.loads(data)
                if github_id(record.get("remote")) == project:
                    remote_specs[project] = record
        overrides = {}
        for entry in entries:
            project = github_id(entry.get("remote"))
            if not project:
                continue
            if entry.get("path"):
                target = Path(entry["path"])
                if not target.is_absolute():
                    raise SyncIssue("项目映射需要本机绝对路径")
                if target.exists() or project not in local:
                    local[project] = target
            remote_specs.setdefault(project, {"remote": entry["remote"], "memoryPath": ".codex/memories"})
            overrides.setdefault(project, {}).update(
                {k: entry[k] for k in ("branch", "memoryPath") if k in entry}
            )
        for project, spec in remote_specs.items():
            if project in local:
                continue
            legacy = self.checkout.joinpath(*project.split("/"))
            if len(found.get(project, [])) > 1:
                errors[project] = "本机有多份相同仓库，请在个人同步配置中选择项目路径"
            elif (legacy / ".git").exists():
                local[project] = legacy
            elif parent and not parent.is_dir():
                errors[project] = "本机项目母目录暂不可用"
            else:
                relative = spec.get("relativePath", project)
                try:
                    if not isinstance(relative, str):
                        raise ValueError
                    base = parent or self.checkout
                    dest = safe_path(base, relative)
                    if any((d / ".git").exists() for d in dest.parents if d.is_relative_to(base)):
                        raise ValueError
                    local[project] = dest
                except ValueError:
                    errors[project] = "项目相对目录不可用，请核对母目录和仓库映射"
        for project, fields in overrides.items():
            if project not in local:
                continue
            spec = remote_specs[project].copy()
            target = local[project]
            if parent and target != parent and target.is_relative_to(parent) and (target / ".git").exists():
                relative = target.relative_to(parent).as_posix()
                safe_path(parent, relative)
                spec.setdefault("relativePath", relative)
            for key, val in fields.items():
                if project not in cached or published.get(project, {}).get(key) != val:
                    spec[key] = val
            safe_path(local[project], spec["memoryPath"])
            if store:
                key = "repo/" + project
                data = pack(spec)
                if digest(data) != index.get(key, EMPTY):
                    store.put(key, index.get(key, EMPTY), data)
                published[project] = fields
            remote_specs[project] = spec
        owners = {}
        for project, path in local.items():
            owners.setdefault(path, []).append(project)
        for path, projects in owners.items():
            if len(projects) > 1 or any(folder in owners and not folder.exists() for folder in path.parents):
                for project in projects:
                    errors[project] = "项目目标目录与另一仓库重叠，请核对项目映射"
        return local, remote_specs

    def cycle(self, stop):
        errors = {}
        channel = (
            ""
            if self.settings.get("codeOnly")
            else self.settings.get("exchange") or (self.coordination or {}).get("url", "")
        )
        scope = hashlib.sha256(
            pack([str(channel), str(self.home), (self.coordination or {}).get("device")])
        ).hexdigest()[:24]
        if self.scope != scope:
            self.scope = scope
            self.poll_cache.clear()
            self.local_state = self.root / ".local" / "personal-sync" / scope
        self.local_state.mkdir(parents=True, exist_ok=True)
        catalog = self.local_state / "catalog.json"
        preferences = self.local_state / "published.json"
        cached = json.loads(catalog.read_bytes()) if catalog.is_file() else {}
        published = json.loads(preferences.read_bytes()) if preferences.is_file() else {}
        store, index = None, {}
        try:
            store = self.store()
            index = store.index() if store else {}
            local, specs = self.bindings(store, index, cached, published, errors)
        except (OSError, ValueError, sqlite3.Error):
            errors["配对"] = "个人同步通道暂不可用"
            store = None
            local, specs = self.bindings(None, {}, cached, published, errors)
        if store:
            for file, data in ((catalog, pack(specs)), (preferences, pack(published))):
                if not file.is_file() or file.read_bytes() != data:
                    atomic_write(file, data)
        with self.lock:
            self.projects = {p: str(path) for p, path in local.items()}
            first = self.priority
            self.priority = None
        ready = {}

        def update(project):
            try:
                path, spec = local[project], specs[project]
                subdir = spec["memoryPath"]
                memory = safe_path(path, subdir)
                if not memory.is_relative_to(path) or subdir.split("/")[0].lower() in (
                    ".git",
                    ".ssh",
                    ".aws",
                ):
                    raise SyncIssue("项目记忆目录无效")
                if store and (path / ".git").exists() and path.resolve() == path:
                    verify_checkout(path, project)
                    exclude_memory(path, subdir)
                    ready[project] = (path, subdir)
                sync_project(path, spec["remote"], spec.get("branch"), memory_subdir=subdir)
                ready[project] = (path, subdir)
                errors[project] = "已同步"
            except (
                ValueError,
                OSError,
                KeyError,
                TypeError,
                sqlite3.Error,
                subprocess.TimeoutExpired,
            ) as error:
                errors[project] = str(error) if isinstance(error, ValueError) else "本机目录或网络暂不可用"

        jobs = queue.Queue()
        for project in sorted(local, key=lambda p: p != first):
            if project not in errors:
                jobs.put(project)

        def consume():
            while not stop.is_set():
                try:
                    project = jobs.get_nowait()
                except queue.Empty:
                    return
                update(project)

        workers = [threading.Thread(target=consume, daemon=True) for _ in range(min(4, len(specs)))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        if store and self.settings.get("memory", True) and not stop.is_set():

            def report(key, error):
                errors.setdefault(
                    "记忆", key + " · " + (str(error) if isinstance(error, ValueError) else "暂不可读写")
                )

            mirror = Mirror(self.local_state, store, self.poll_cache)
            try:
                sessions = sync_memories(mirror, index, self.home, ready, self.poll_cache, report, stop)
                for project, (root, _) in ready.items():
                    records = {
                        tid: {"cwd": s["relative"], "updated": s["updated"]}
                        for tid, s in sessions.items()
                        if s["project"] == project
                    }
                    marker = (
                        Path(value(root, "rev-parse", "--absolute-git-dir")) / "lanxi-personal-sessions.json"
                    )
                    data = pack(records)
                    if not stop.is_set() and (not marker.is_file() or marker.read_bytes() != data):
                        atomic_write(marker, data)
                with self.lock:
                    self.threads = sessions
            finally:
                mirror.db.close()
        with self.lock:
            for project in specs:
                self.completed[project] = self.completed.get(project, 0) + 1
            self.epoch += 1
            self.status = errors

    def worker(self, stop):
        interval = max(2, min(300, int(self.settings.get("intervalSeconds", 10))))
        while not stop.is_set():
            self.wake.clear()
            try:
                self.cycle(stop)
            except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, subprocess.TimeoutExpired):
                with self.lock:
                    self.epoch += 1
                    self.status = {"配对": "个人同步暂不可用，请检查项目登记和配对连接"}
            for _ in range(interval * 4):
                if stop.wait(0.25) or self.wake.is_set():
                    break
