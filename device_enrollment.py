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
SETUP_SECONDS = 1800
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
                CREATE TABLE IF NOT EXISTS device_setups (
                    code_hash TEXT PRIMARY KEY, space TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_setup_members (
                    device TEXT PRIMARY KEY, code_hash TEXT NOT NULL, token_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_grants (
                    code_hash TEXT PRIMARY KEY, space TEXT NOT NULL
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

    def issue_setup(self, space):
        code = secrets.token_urlsafe(32)
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM device_setup_members WHERE code_hash IN (SELECT code_hash FROM device_setups WHERE expires<=?)", (now,))
            db.execute("DELETE FROM device_setups WHERE expires<=?", (now,))
            db.execute("INSERT INTO device_setups VALUES(?,?,?)", (sha(code), space, now + SETUP_SECONDS))
        return code

    def issue_workspace(self, space):
        code = secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO workspace_grants VALUES(?,?)", (sha(code), space))
        return code

    def revoke_workspace(self, space):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM workspace_grants WHERE space=?", (space,))

    def join_workspace(self, claim):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            grant = db.execute("SELECT space FROM workspace_grants WHERE code_hash=?", (sha(claim.code),)).fetchone()
            if not grant:
                raise HTTPException(403, "私人工作区授权无效")
            if claim.device in self.devices:
                raise HTTPException(409, "设备标识已登记")
            current = db.execute("SELECT space, token_hash FROM enrolled_devices WHERE device=?",
                                 (claim.device,)).fetchone()
            credentials = (grant[0], sha(claim.token))
            if current == credentials:
                return
            if current:
                raise HTTPException(409, "设备标识已登记")
            db.execute("INSERT INTO enrolled_devices VALUES(?,?,?)", (claim.device, *credentials))

    def join_setup(self, claim):
        key, token = sha(claim.code), sha(claim.token)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            setup = db.execute("SELECT space, expires FROM device_setups WHERE code_hash=?", (key,)).fetchone()
            if not setup or setup[1] <= time.time():
                raise HTTPException(403, "双机设置已过期或无效")
            space = setup[0]
            existing = db.execute(
                "SELECT code_hash, token_hash FROM device_setup_members WHERE device=?", (claim.device,),
            ).fetchone()
            enrolled = db.execute(
                "SELECT space, token_hash FROM enrolled_devices WHERE device=?", (claim.device,),
            ).fetchone()
            if existing == (key, token):
                return "paired" if enrolled == (space, token) else "waiting"
            if existing or enrolled or claim.device in self.devices:
                raise HTTPException(409, "设备标识已登记")
            members = db.execute(
                "SELECT device, token_hash FROM device_setup_members WHERE code_hash=?", (key,),
            ).fetchall()
            if len(members) == 2 or any(row[1] == token for row in members):
                raise HTTPException(409, "双机设置已满或重复申请")
            db.execute("INSERT INTO device_setup_members VALUES(?,?,?)", (claim.device, key, token))
            if not members:
                return "waiting"
            try:
                db.executemany("INSERT INTO enrolled_devices VALUES(?,?,?)",
                               ((device, space, token_hash) for device, token_hash in [*members, (claim.device, token)]))
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "设备标识已登记") from error
            db.execute("UPDATE device_setups SET expires=? WHERE code_hash=?", (int(time.time()) + SETUP_SECONDS, key))
            return "paired"


def install_pairing(app, enrollment):
    from fastapi.responses import JSONResponse

    @app.post("/api/v1/pairing/invite")
    def invite(x_lanxi_device: str = Header(default=""), authorization: str = Header(default="")):
        space = enrollment.space(x_lanxi_device, authorization)
        if not space:
            raise HTTPException(403, "本机尚未配对")
        return JSONResponse(enrollment.issue(x_lanxi_device, space), headers={"Cache-Control": "no-store"})

    async def read_claim(request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 512:
                raise HTTPException(413, "配对请求过大")
            data.extend(chunk)
        try:
            return Redemption.model_validate_json(data)
        except ValidationError as error:
            raise HTTPException(422, "配对请求格式无效") from error
    @app.post("/api/v1/pairing/redeem")
    async def redeem(request: Request):
        claim = await read_claim(request)
        await run_in_threadpool(enrollment.redeem, claim)
        return JSONResponse({"device": claim.device}, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/pairing/join-setup")
    async def join_setup(request: Request):
        claim = await read_claim(request)
        state = await run_in_threadpool(enrollment.join_setup, claim)
        return JSONResponse({"device": claim.device, "state": state}, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/pairing/join-workspace")
    async def join_workspace(request: Request):
        claim = await read_claim(request)
        await run_in_threadpool(enrollment.join_workspace, claim)
        return JSONResponse({"device": claim.device}, headers={"Cache-Control": "no-store"})
