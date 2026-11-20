"""Issue and redeem expiring invitations to a coordinator's personal space."""

import hashlib
import secrets
import sqlite3
import time
from contextlib import closing

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

INVITE_SECONDS = 600
SECRET_PATTERN = r"^[A-Za-z0-9_-]{43}$"
DEVICE_PATTERN = r"^lanxi-[0-9a-f]{24}$"


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Redemption(BaseModel):
    code: str = Field(pattern=SECRET_PATTERN, max_length=43)
    device: str = Field(pattern=DEVICE_PATTERN, max_length=30)
    token: str = Field(pattern=SECRET_PATTERN, max_length=43)


class Enrollment:
    def __init__(self, path, devices):
        self.path = path
        self.devices = devices
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS device_invites (
                    code_hash TEXT PRIMARY KEY, space TEXT NOT NULL,
                    issuer TEXT NOT NULL, expires INTEGER NOT NULL, used_by TEXT
                );
                CREATE TABLE IF NOT EXISTS enrolled_devices (
                    device TEXT PRIMARY KEY, space TEXT NOT NULL, token_hash TEXT NOT NULL
                );
            """)

    def space(self, device, authorization):
        if not authorization.startswith("Bearer "):
            return None
        token = authorization.removeprefix("Bearer ")
        entry = self.devices.get(device)
        if entry is not None:
            if secrets.compare_digest(token.encode(), entry["token"].encode()):
                return entry.get("personalSpace")
            return None
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT space, token_hash FROM enrolled_devices WHERE device=?", (device,)
            ).fetchone()
        if row and secrets.compare_digest(sha(token), row[1]):
            return row[0]
        return None

    def issue(self, issuer, space):
        code = secrets.token_urlsafe(32)
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM device_invites WHERE expires<=? OR issuer=?", (now, issuer))
            db.execute("INSERT INTO device_invites VALUES(?,?,?,?,NULL)",
                       (sha(code), space, issuer, now + INVITE_SECONDS))
        return {"code": code, "expiresIn": INVITE_SECONDS}

    def redeem(self, claim):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT space, expires, used_by FROM device_invites WHERE code_hash=?",
                             (sha(claim.code),)).fetchone()
            if not row or row[1] <= time.time() or row[2] not in (None, claim.device):
                raise HTTPException(403, "配对码无效、已使用或已过期")
            if claim.device in self.devices:
                raise HTTPException(409, "设备标识已登记")
            current = db.execute("SELECT space, token_hash FROM enrolled_devices WHERE device=?",
                                 (claim.device,)).fetchone()
            credentials = (row[0], sha(claim.token))
            if row[2] == claim.device and current == credentials:
                return
            if current is not None or row[2] is not None:
                raise HTTPException(409, "设备标识已登记")
            db.execute("INSERT INTO enrolled_devices VALUES(?,?,?)", (claim.device, *credentials))
            db.execute("UPDATE device_invites SET used_by=? WHERE code_hash=?", (claim.device, sha(claim.code)))


def install_pairing(app, enrollment):
    from fastapi.responses import JSONResponse

    @app.post("/api/v1/pairing/invite")
    def invite(x_lanxi_device: str = Header(default=""), authorization: str = Header(default="")):
        space = enrollment.space(x_lanxi_device, authorization)
        if not space:
            raise HTTPException(403, "本机尚未配对")
        return JSONResponse(enrollment.issue(x_lanxi_device, space), headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/pairing/redeem")
    async def redeem(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 512:
                raise HTTPException(413, "配对请求过大")
            data.extend(chunk)
        try:
            claim = Redemption.model_validate_json(data)
        except ValidationError as error:
            raise HTTPException(422, "配对请求格式无效") from error
        await run_in_threadpool(enrollment.redeem, claim)
        return JSONResponse({"device": claim.device}, headers={"Cache-Control": "no-store"})
