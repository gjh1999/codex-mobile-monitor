"""Run the coordination API separately from the workstation on the home node."""
import argparse
import json
import secrets
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from compute_queue import ComputeQueue, validate_config
from coordination import Conflict, Ownership


class Activity(BaseModel):
    session: str = Field(min_length=1, max_length=128)
    source: Literal['vscode', 'pycharm', 'cli', 'switch']
    sequence: int = Field(ge=1, le=2**53 - 1, strict=True)
    event: Literal['open', 'heartbeat', 'close']


class Reserve(BaseModel):
    instance: str = Field(min_length=1, max_length=128)
    thread: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=0, le=2**53 - 1, strict=True)
    ticket: str | None = Field(default=None, min_length=32, max_length=128)


class Release(BaseModel):
    ticket: str = Field(min_length=32, max_length=128)


JobId = Annotated[str, Field(pattern=r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', max_length=36)]
Commit = Annotated[str, Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$', max_length=64)]
Profile = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,80}$', max_length=80)]


class ComputeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ComputeSubmit(ComputeInput):
    id: JobId
    session: str = Field(min_length=1, max_length=128)
    profile: Profile
    commit: Commit


class ComputeClaim(ComputeInput):
    instance: JobId


class ComputeHeartbeat(ComputeClaim):
    profiles: list[Profile] = Field(max_length=64)
    commits: list[Commit] = Field(max_length=64)
    cpu: int = Field(ge=0, le=1024, strict=True)
    memoryMiB: int = Field(ge=0, le=16 * 1024**2, strict=True)
    gpuVramMiB: list[Annotated[int, Field(ge=0, le=1024**2, strict=True)]] = Field(max_length=64)


class ComputeEvent(ComputeClaim):
    ticket: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', max_length=43)
    event: Literal['start', 'renew', 'finish']
    exitCode: int | None = Field(default=None, ge=-(2**31), le=2**32 - 1, strict=True)
    resultSha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$', max_length=64)


def create_app(path, devices, clock=None, compute=None):
    tokens = [entry['token'].encode() for entry in devices.values()]
    if any(len(token) < 32 for token in tokens) or len(set(tokens)) != len(tokens):
        raise ValueError('每台设备需要独立的随机令牌，至少 32 字节')
    store = Ownership(path, **({'clock': clock} if clock else {}))
    if compute is not None:
        validate_config(compute, devices)
        jobs = ComputeQueue(store, compute)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Conflict)
    async def conflict(_, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    def authorize(project: str, x_lanxi_device: str = Header(default=''), authorization: str = Header(default='')):
        entry = devices.get(x_lanxi_device)
        scheme, _, token = authorization.partition(' ')
        if not entry or scheme.lower() != 'bearer' or not secrets.compare_digest(
                token.encode(), entry['token'].encode()):
            raise HTTPException(401, '设备认证失败')
        if project not in entry['projects']:
            raise HTTPException(403, '设备未登记此项目')
        return x_lanxi_device

    prefix = '/api/v1/projects/{project}'

    @app.get(prefix + '/control')
    def status(project: str, device=Depends(authorize)):
        return store.status(project)

    @app.post(prefix + '/activity')
    def activity(project: str, body: Activity, device=Depends(authorize)):
        return store.report(project, device, **body.model_dump())

    @app.post(prefix + '/writes')
    def reserve(project: str, body: Reserve, device=Depends(authorize)):
        return {'ticket': store.reserve(project, device, **body.model_dump())}

    @app.post(prefix + '/writes/release')
    def release(project: str, body: Release, device=Depends(authorize)):
        store.release(project, device, body.ticket)
        return {'released': True}

    if compute is not None:
        def authorizes_compute(project: str, device=Depends(authorize)):
            if project not in compute:
                raise HTTPException(404, '项目未启用计算队列')
            return device

        def authorizes_worker(project: str, device=Depends(authorizes_compute)):
            if device not in compute[project]['workers']:
                raise HTTPException(403, '设备未登记为此项目的计算端')
            return device

        @app.post(prefix + '/compute/jobs')
        def submit_job(project: str, body: ComputeSubmit, device=Depends(authorizes_compute)):
            return jobs.submit(project, device, body.id, body.session, body.profile, body.commit)

        @app.get(prefix + '/compute/jobs/{job_id}')
        def job_status(project: str, job_id: JobId, device=Depends(authorizes_compute)):
            result = jobs.status(project, device, job_id)
            if result is None:
                raise HTTPException(404, '作业不存在或当前设备不可见')
            return result

        @app.post(prefix + '/compute/heartbeat')
        def compute_heartbeat(project: str, body: ComputeHeartbeat, device=Depends(authorizes_worker)):
            jobs.heartbeat(project, device, body.instance, body.model_dump(exclude={'instance'}))
            return {'accepted': True}

        @app.post(prefix + '/compute/claim')
        def claim_job(project: str, body: ComputeClaim, device=Depends(authorizes_worker)):
            return {'job': jobs.claim(project, device, body.instance)}

        @app.post(prefix + '/compute/jobs/{job_id}/events')
        def job_event(project: str, job_id: JobId, body: ComputeEvent, device=Depends(authorizes_worker)):
            result_fields = (body.exitCode is not None, body.resultSha256 is not None)
            expected = (True, True) if body.event == 'finish' else (False, False)
            if result_fields != expected:
                raise HTTPException(422, '仅完成事件携带退出码和结果清单散列，两者均必需')
            return jobs.update(project, device, job_id, body.instance, body.ticket, body.event,
                               body.exitCode, body.resultSha256)

    return app


if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--devices', type=Path)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--compute', type=Path)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--inspect', metavar='PROJECT')
    action.add_argument('--release-confirmed', nargs=4, metavar=('PROJECT', 'VERSION', 'DEVICE', 'THREAD'))
    action.add_argument('--confirm-compute-stopped', nargs=4, metavar=('PROJECT', 'JOB', 'ATTEMPT', 'DEVICE'))
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(Ownership(args.database).status(args.inspect), ensure_ascii=True))
    elif args.release_confirmed:
        project, version, device, thread = args.release_confirmed
        try:
            Ownership(args.database).release_confirmed(project, int(version), device, thread)
        except ValueError as error:
            parser.exit(1, f'{error}\n')
    elif args.confirm_compute_stopped:
        project, job, attempt, device = args.confirm_compute_stopped
        try:
            print(json.dumps(ComputeQueue(Ownership(args.database), {}).confirm_stopped(project, job, int(attempt), device)))
        except ValueError as error:
            parser.exit(1, f'{error}\n')
    else:
        if not args.devices:
            parser.error('启动服务时需要 --devices')
        devices = json.loads(args.devices.read_text(encoding='utf-8-sig'))
        compute = json.loads(args.compute.read_text(encoding='utf-8-sig')) if args.compute else None
        uvicorn.run(create_app(args.database, devices, compute=compute), host=args.host, port=args.port)
