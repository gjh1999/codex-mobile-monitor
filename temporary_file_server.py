"""Relay private, explicitly requested large files and keep only completion receipts."""

import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path as DiskPath
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from compute_artifacts import ArtifactStore, CHUNK, append_block
from coordination import Conflict, Ownership
from handoff import local_path

RESERVE = 5 * 1024**3
Id = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
Part = Literal["original", "edited"]


class Application(BaseModel):
    project: str = Field(pattern=r"^[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}$")
    source: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    path: str = Field(min_length=1, max_length=360)


class FileInfo(BaseModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0, le=2**53, strict=True)


class Receipt(BaseModel):
    event: Literal["downloaded", "applied", "conflict"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class TemporaryFiles(Ownership):
    def __init__(self, store, directory):
        self.path = store.path
        self.files = ArtifactStore(directory)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS temporary_file_requests (
                key TEXT PRIMARY KEY, space TEXT NOT NULL, project TEXT NOT NULL, name TEXT NOT NULL,
                source TEXT NOT NULL, receiver TEXT NOT NULL, state TEXT NOT NULL, updated INTEGER NOT NULL,
                original TEXT, edited TEXT, cleaned INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS temporary_file_active
                          ON temporary_file_requests(space,project,name COLLATE NOCASE)
                          WHERE state NOT IN ('completed','conflict')""")

    @staticmethod
    def payload(row):
        return {**{k: row[k] for k in ("key", "project", "name", "source", "receiver", "state", "updated")},
                **{k: json.loads(row[k]) if row[k] else None for k in ("original", "edited")}}

    def requests(self, space, device):
        with self.transaction() as db:
            rows = db.execute("""SELECT * FROM temporary_file_requests WHERE space=? AND (source=? OR receiver=?)
                                 ORDER BY updated DESC LIMIT 100""", (space, device, device))
            rows = list(rows)
        for row in rows:
            if row["state"] in ("completed", "conflict") and not row["cleaned"]:
                self.clean(row["key"])
        return [self.payload(row) for row in rows]

    def create(self, space, device, request):
        if request.source == device:
            raise Conflict("请选择另一台设备。")
        local_path(self.files.root / "names", request.path)
        with self.transaction() as db:
            try:
                listed = db.execute("SELECT data FROM personal_records WHERE space=? AND key=?", (
                    space, f"large-inventory/{request.project}/{request.source}",
                )).fetchone()
                allowed = listed and listed["data"] and request.path in json.loads(listed["data"])["files"]
            except (sqlite3.OperationalError, ValueError, TypeError, KeyError):
                allowed = False
            if not allowed:
                raise Conflict("来源电脑尚未登记此文件。")
            active = db.execute("""SELECT * FROM temporary_file_requests WHERE space=? AND project=? AND name=? COLLATE NOCASE
                                  AND state NOT IN ('completed','conflict')""",
                                (space, request.project, request.path)).fetchone()
            if active:
                if active["source"] == request.source and active["receiver"] == device:
                    return self.payload(active)
                raise Conflict("此文件已有未完成的借用。")
            key = uuid4().hex
            db.execute("INSERT INTO temporary_file_requests VALUES(?,?,?,?,?,?,'requested',?,NULL,NULL,0)",
                       (key, space, request.project, request.path, request.source, device, int(time.time())))
            return self.payload(self.entry(db, space, key, device))

    @staticmethod
    def entry(db, space, key, device):
        row = db.execute("SELECT * FROM temporary_file_requests WHERE key=? AND space=?", (key, space)).fetchone()
        if not row or device not in (row["source"], row["receiver"]):
            raise HTTPException(404, "找不到此设备的借用记录")
        return row

    def directory(self, key, part):
        directory = self.files.root / key / part
        directory.mkdir(exist_ok=True, parents=True)
        return directory

    def item(self, row, part):
        info = json.loads(row[part]) if row[part] else None
        if not info:
            raise Conflict("文件还在准备中。")
        return {"sha256": info["sha256"], "bytes": info["size"]}

    @contextmanager
    def upload_lock(self, space, device, key, part, role, stages):
        # Keep long checksum work outside the coordinator's SQLite transaction.
        def verify():
            with self.transaction() as db:
                row = self.entry(db, space, key, device)
                if row[role] != device or row["state"] not in stages:
                    raise Conflict("当前设备或步骤无法上传这个版本。")
                return row
        verify()
        directory = self.directory(key, part)
        with self.files.locked(directory):
            yield directory, verify()

    def upload_info(self, space, device, key, part, info):
        item = {"sha256": info.sha256, "bytes": info.size}
        start, writing, done, role = self.upload_states(part)
        with self.upload_lock(space, device, key, part, role, (start, writing, done)) as (directory, row):
            raw = json.dumps(info.model_dump(), sort_keys=True)
            if row[part] and row[part] != raw:
                if row["state"] != writing:
                    raise Conflict("另一端已取得此版本，请重新发起借用。")
                for path in self.files.paths(directory, 0):
                    path.unlink(missing_ok=True)
            offset = self.files.progress(directory, 0, item)
            if shutil.disk_usage(directory).free < RESERVE + info.size - offset:
                raise HTTPException(507, "服务器临时空间不足")
            with self.transaction() as db:
                db.execute(f"UPDATE temporary_file_requests SET {part}=?,state=?,updated=? WHERE key=?",
                           (raw, done if offset == info.size else writing, int(time.time()), key))
            return {"offset": offset}

    @staticmethod
    def upload_states(part):
        if part == "original":
            return "requested", "uploading", "ready", "source"
        return "editing", "returning", "returned", "receiver"

    def upload(self, space, device, key, part, offset, data):
        _, writing, done, role = self.upload_states(part)
        with self.upload_lock(space, device, key, part, role, (writing, done)) as (directory, row):
            item = self.item(row, part)
            current = self.files.progress(directory, 0, item)
            if offset < current:
                return {"offset": current}
            if offset != current or len(data) != min(CHUNK, item["bytes"] - current) or not data:
                raise Conflict("偏移已变化，请重新读取进度。")
            if shutil.disk_usage(directory).free < RESERVE + len(data):
                raise HTTPException(507, "服务器临时空间不足")
            append_block(self.files.paths(directory, 0)[0], data)
            current = self.files.progress(directory, 0, item)
            if current == item["bytes"]:
                with self.transaction() as db:
                    db.execute("UPDATE temporary_file_requests SET state=?,updated=? WHERE key=?",
                               (done, int(time.time()), key))
            return {"offset": current}

    def download(self, space, device, key, part, offset):
        with self.transaction() as db:
            row = self.entry(db, space, key, device)
            allowed = (part == "original" and row["receiver"] == device and row["state"] in ("ready", "editing")
                       or part == "edited" and row["source"] == device and row["state"] == "returned")
            if not allowed:
                raise Conflict("当前步骤无法下载这个版本。")
            item = self.item(row, part)
            if not 0 <= offset < item["bytes"]:
                raise HTTPException(416, "偏移超出文件大小")
        with self.files.available(self.directory(key, part), 0, item).open("rb") as stream:
            stream.seek(offset)
            return stream.read(min(CHUNK, item["bytes"] - offset))

    def receipt(self, space, device, key, receipt):
        transitions = {"downloaded": ("ready", "editing", "receiver", "original"),
                       "applied": ("returned", "completed", "source", "edited"),
                       "conflict": ("returned", "conflict", "source", "edited")}
        before, after, role, part = transitions[receipt.event]
        with self.transaction() as db:
            row = self.entry(db, space, key, device)
            if row[role] != device or row["state"] not in (before, after) or self.item(row, part)["sha256"] != receipt.sha256:
                raise Conflict("交接确认与设备或文件版本不符。")
            db.execute("UPDATE temporary_file_requests SET state=?,updated=? WHERE key=?",
                       (after, int(time.time()), key))
        if after in ("completed", "conflict"):
            self.clean(key)

    def clean(self, key):
        for part in ("original", "edited"):
            path = self.files.root / key / part
            for file in self.files.paths(path, 0):
                file.unlink(missing_ok=True)
            (path / "lockfile").unlink(missing_ok=True)
            if path.exists():
                path.rmdir()
        root = self.files.root / key
        if root.exists():
            root.rmdir()
        with self.transaction() as db:
            db.execute("UPDATE temporary_file_requests SET cleaned=1 WHERE key=?", (key,))


def install_temporary_files(app, store, directory: DiskPath, enrollment):
    relay = TemporaryFiles(store, directory)

    def identity(x_lanxi_device: str = Header(default=""), authorization: str = Header(default="")):
        space = enrollment.space(x_lanxi_device, authorization)
        if not space:
            raise HTTPException(403, "此设备未配对个人空间")
        return space, x_lanxi_device

    def guarded(action, *args):
        try:
            return action(*args)
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        except FileNotFoundError as error:
            raise HTTPException(404, "临时文件尚未准备") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except OSError as error:
            raise HTTPException(503, "临时文件存储暂不可用") from error

    user = Depends(identity)
    api = "/api/v1/temporary-files"

    @app.get(api)
    def requests(auth=user):
        return guarded(relay.requests, *auth)

    @app.post(api)
    def create(application: Application, auth=user):
        return guarded(relay.create, *auth, application)

    @app.post(api + "/{key}/receipt")
    def receipt(key: Id, confirmation: Receipt, auth=user):
        guarded(relay.receipt, *auth, key, confirmation)
        return {"acknowledged": True}

    @app.post(api + "/{key}/{part}")
    def begin(key: Id, part: Part, info: FileInfo, auth=user):
        return guarded(relay.upload_info, *auth, key, part, info)

    @app.put(api + "/{key}/{part}")
    async def upload(key: Id, part: Part, request: Request, offset: Annotated[int, Query(ge=0)], auth=user):
        data = bytearray()
        async for block in request.stream():
            if len(data) + len(block) > CHUNK:
                raise HTTPException(413, "分块过大")
            data.extend(block)
        return await run_in_threadpool(guarded, relay.upload, *auth, key, part, offset, data)

    @app.get(api + "/{key}/{part}")
    def download(key: Id, part: Part, offset: Annotated[int, Query(ge=0)], auth=user):
        return Response(guarded(relay.download, *auth, key, part, offset), media_type="application/octet-stream")

