"""Versioned private records shared only by the paired devices of one person."""

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request

MAX_RECORD = 10 * 1024**2
EMPTY = "-"


class Changed(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else EMPTY


class Records:
    def __init__(self, path, space):
        self.path, self.space = path, space
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS personal_records(space TEXT,key TEXT,hash TEXT,data BLOB,PRIMARY KEY(space,key))"
            )

    def index(self):
        with closing(sqlite3.connect(self.path)) as db:
            return dict(db.execute("SELECT key,hash FROM personal_records WHERE space=?", (self.space,)))

    def read(self, key):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT data FROM personal_records WHERE space=? AND key=?", (self.space, key)
            ).fetchone()
            return row[0] if row else None

    def put(self, key, previous, data):
        if len(key) > 650 or data is not None and len(data) >= MAX_RECORD:
            raise ValueError("同步记录过大")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT hash FROM personal_records WHERE space=? AND key=?", (self.space, key)
            ).fetchone()
            if (row[0] if row else EMPTY) != previous:
                raise Changed("另一台设备刚刚更新了记录")
            db.execute(
                "INSERT OR REPLACE INTO personal_records VALUES(?,?,?,?)",
                (self.space, key, digest(data), data),
            )


class RemoteRecords:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, data=None):
        request = Request(
            self.client.url + "/api/v1/personal/" + path,
            method=method,
            data=data,
            headers={
                "Authorization": "Bearer " + (self.client.token or ""),
                "X-Lanxi-Device": self.client.device,
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            with self.client.opener.open(request, timeout=10) as response:
                result = response.read(MAX_RECORD + 1)
                if len(result) > MAX_RECORD:
                    raise ValueError("接收记录超过上限")
                return result
        except HTTPError as error:
            error.close()
            if error.code == 409:
                raise Changed("远端已更新") from error
            raise

    def index(self):
        return json.loads(self.request("GET", "index"))

    def read(self, key):
        return self.request("GET", "record?" + urlencode({"key": key}))

    def put(self, key, previous, data):
        self.request(
            "DELETE" if data is None else "PUT",
            "record?" + urlencode({"key": key, "previous": previous}),
            data,
        )


def install_routes(app, path, devices):
    from fastapi import Depends, Header, HTTPException
    from fastapi import Request as APIRequest
    from fastapi.responses import Response
    from starlette.concurrency import run_in_threadpool

    def authorize(x_lanxi_device: str = Header(default=""), authorization: str = Header(default="")):
        entry = devices.get(x_lanxi_device) or {}
        if not entry.get("personalSpace") or not secrets.compare_digest(
            authorization.encode(), ("Bearer " + entry["token"]).encode()
        ):
            raise HTTPException(403, "此设备未配对个人空间")
        return Records(path, entry["personalSpace"])

    authorized = Depends(authorize)

    @app.get("/api/v1/personal/index")
    def index(store=authorized):
        return store.index()

    @app.get("/api/v1/personal/record")
    def read(key: str, store=authorized):
        data = store.read(key)
        if data is None:
            raise HTTPException(404)
        return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    @app.api_route("/api/v1/personal/record", methods=["PUT", "DELETE"])
    async def write(request: APIRequest, key: str, previous: str, store=authorized):
        if len(key) > 650 or not re.fullmatch(r"-|[a-f0-9]{64}", previous):
            raise HTTPException(422)
        content = bytearray()
        async for block in request.stream():
            if len(content) + len(block) >= MAX_RECORD:
                raise HTTPException(413)
            content.extend(block)
        try:
            await run_in_threadpool(
                store.put, key, previous, None if request.method == "DELETE" else bytes(content)
            )
        except Changed:
            raise HTTPException(409)
        return Response(status_code=204)
