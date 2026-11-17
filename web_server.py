"""Authenticated web adapter for the existing desktop workers (one process only)."""
import asyncio
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import monitor


def create_app(start_workers=True):
    password = os.environ.get('MONITOR_PASSWORD', '')
    if len(password) < 16:
        raise RuntimeError('Set MONITOR_PASSWORD to at least 16 characters')
    sessions, attempts = {}, {}

    @asynccontextmanager
    async def lifespan(app):
        stop = threading.Event()
        worker = monitor.start_background_service(stop)[1] if start_workers else None
        yield
        stop.set()
        if worker:
            await asyncio.to_thread(worker.join, 8)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    def auth(request: Request):
        if sessions.get(request.cookies.get('monitor_session', ''), 0) < time.time():
            raise HTTPException(401, '请先登录')
        if request.method != 'GET' and request.headers.get('x-monitor-request') != '1':
            raise HTTPException(403, '请求校验失败')

    @app.middleware('http')
    async def security(request, call_next):
        if request.method != 'GET':
            origin = request.headers.get('origin')
            expected = os.environ.get('MONITOR_ORIGIN', str(request.base_url).rstrip('/'))
            if origin and origin != expected:
                return Response('Invalid origin', status_code=403)
            if int(request.headers.get('content-length', '0')) > 65536:
                return Response('Too large', status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        return response

    class Login(BaseModel):
        password: str = Field(max_length=1024)

    @app.post('/api/login')
    def login(data: Login, request: Request, response: Response):
        key = request.client.host
        now = time.time()
        for ip in list(attempts):
            if now - attempts[ip][0] > 60:
                del attempts[ip]
        stamp, count = attempts.get(key, (now, 0))
        if count >= 10:
            raise HTTPException(429, '请一分钟后重试')
        attempts[key] = (stamp, count + 1)
        if not secrets.compare_digest(data.password.encode(), password.encode()):
            raise HTTPException(401, '密码不正确')
        for token in list(sessions):
            if sessions[token] < now:
                del sessions[token]
        token = secrets.token_urlsafe(32)
        sessions[token] = now + 43200
        response.set_cookie('monitor_session', token, httponly=True, samesite='strict',
                            secure=os.environ.get('MONITOR_HTTPS') == '1', max_age=43200)
        return {'ok': True}

    @app.post('/api/logout', dependencies=[Depends(auth)])
    def logout(request: Request, response: Response):
        sessions.pop(request.cookies.get('monitor_session'), None)
        response.delete_cookie('monitor_session')
        return {'ok': True}

    @app.get('/api/state', dependencies=[Depends(auth)])
    def state(client: Literal['codex', 'claude'] = 'codex'):
        return monitor.client_snapshot(monitor.snapshot(), client)

    @app.get('/api/conversation/{thread}', dependencies=[Depends(auth)])
    def conversation(thread: str, client: Literal['codex', 'claude'] = 'codex'):
        return monitor.conversation_for(thread, client)

    class Operation(BaseModel):
        client: Literal['codex', 'claude']
        thread: str = Field(max_length=100)
        action: Literal['create_test', 'attach', 'detach', 'manage', 'unmanage', 'pause', 'resume', 'message', 'rename']
        text: str = Field(default='', max_length=10000)
        request_id: str = Field(min_length=16, max_length=100)

    @app.post('/api/operations', dependencies=[Depends(auth)])
    def operate(data: Operation):
        controller = monitor.get_controller(data.client)
        if controller is None:
            raise HTTPException(503, '后台正在连接，请稍后重试')
        try:
            return {'id': controller.submit(data.thread, data.action, data.text, data.request_id)}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    class Decision(BaseModel):
        token: str = Field(max_length=200)
        allow: bool

    @app.post('/api/approvals', dependencies=[Depends(auth)])
    def decide(data: Decision):
        try:
            result = monitor.approvals.decide(data.token, data.allow)
            if result not in ('allowed', 'denied'):
                raise HTTPException(409, f'审批结果：{result}，请检查任务')
            return {'result': result}
        except (KeyError, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    dist = Path(__file__).parent / 'web' / 'dist'
    if dist.exists():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get('MONITOR_HOST', '127.0.0.1'), port=8765)
