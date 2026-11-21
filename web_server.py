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
from pydantic import BaseModel, Field, StrictBool
import monitor
from handoff import handoff_receipts
from handoff_sync import read_state as read_handoff_sync
from coordination_client import ControlDenied, device_client


def create_app(start_workers=True):
    password = os.environ.get('MONITOR_PASSWORD', '')
    if len(password) < 16:
        raise RuntimeError('Set MONITOR_PASSWORD to at least 16 characters')
    sessions, attempts = {}, {}

    @asynccontextmanager
    async def lifespan(app):
        stop = threading.Event()
        worker = monitor.start_background_service(stop, embed_web=False)[1] if start_workers else None
        try:
            yield
        finally:
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

    @app.get('/api/v1/handoffs', dependencies=[Depends(auth)])
    def handoffs():
        projects = monitor.config().get('handoffProjects', {})
        if not isinstance(projects, dict):
            raise HTTPException(503, 'handoffProjects 配置应为项目与本机目录的映射')
        return {'projects': handoff_receipts(projects)}

    @app.get('/api/v1/handoff-sync', dependencies=[Depends(auth)])
    def handoff_sync():
        try:
            configured = monitor.config().get('handoffSync')
            health = (monitor.snapshot().get('handoffSyncStatus') or 'awaiting_scan') if configured else 'disabled'
            state = read_handoff_sync(monitor.ROOT) if configured else {'projects': {}}
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise HTTPException(503, '同步状态暂不可用') from error
        return {**state, 'status': health}

    @app.get('/api/v1/project-control', dependencies=[Depends(auth)])
    def project_control():
        try:
            client = device_client(monitor.config())
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise HTTPException(503, '协调服务配置有误') from error
        projects = []
        if client:
            for project in client.paths.values():
                try:
                    projects.append({'project': project, **client.request(project, 'control')})
                except ControlDenied:
                    projects.append({'project': project, 'status': 'unavailable'})
        return {'projects': projects}

    @app.get('/api/conversation/{thread}', dependencies=[Depends(auth)])
    def conversation(thread: str, client: Literal['codex', 'claude'] = 'codex'):
        return monitor.conversation_for(thread, client, local_history=False)

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
            result = (monitor.decide_approvals([data.token], data.allow)[0]['decision'] if data.token.startswith('remote:')
                      else monitor.approvals.decide(data.token, data.allow))
            if result not in ('allowed', 'denied', 'queued'):
                raise HTTPException(409, f'审批结果：{result}，请检查任务')
            return {'result': result}
        except (KeyError, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    class ApprovalBatch(BaseModel):
        tokens: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1, max_length=256)
        allow: StrictBool

    @app.get('/api/approvals', dependencies=[Depends(auth)])
    def approval_queue():
        state = monitor.snapshot()
        return {'pending': monitor.approval_queue(), 'decisions': monitor.workspace_codex.decisions(),
                'sharedCodex': state.get('codexApprovalWatch', 'own_connection'),
                'workspace': state.get('codexWorkspaceWatch', 'local')}

    @app.post('/api/approvals/batch', dependencies=[Depends(auth)])
    def decide_batch(data: ApprovalBatch):
        return {'results': monitor.decide_approvals(data.tokens, data.allow)}

    dist = Path(__file__).parent / 'web' / 'dist'
    if dist.exists():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app


def embedded_web_worker(stop):
    """Serve the authenticated Web UI against the desktop's live monitor state."""
    import uvicorn
    config = uvicorn.Config(create_app(start_workers=False), host=os.environ.get('MONITOR_HOST', '127.0.0.1'),
                            port=int(os.environ.get('MONITOR_PORT', 8765)), timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)

    async def serve():
        task = asyncio.create_task(server.serve())
        while not stop.is_set() and not task.done():
            await asyncio.sleep(0.2)
        server.should_exit = True
        await task

    return threading.Thread(target=lambda: asyncio.run(serve()), name='monitor-web', daemon=True)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get('MONITOR_HOST', '127.0.0.1'),
                port=int(os.environ.get('MONITOR_PORT', 8765)))
