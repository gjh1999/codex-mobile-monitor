"""Run the coordination API separately from the workstation on the home node."""
import argparse
import json
import secrets
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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


def create_app(path, devices, clock=None):
    tokens = [entry['token'].encode() for entry in devices.values()]
    if any(len(token) < 32 for token in tokens) or len(set(tokens)) != len(tokens):
        raise ValueError('每台设备需要独立的随机令牌，至少 32 字节')
    store = Ownership(path, **({'clock': clock} if clock else {}))
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

    return app


if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--devices', type=Path)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--inspect', metavar='PROJECT')
    action.add_argument('--release-confirmed', nargs=4, metavar=('PROJECT', 'VERSION', 'DEVICE', 'THREAD'))
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(Ownership(args.database).status(args.inspect), ensure_ascii=True))
    elif args.release_confirmed:
        project, version, device, thread = args.release_confirmed
        try:
            Ownership(args.database).release_confirmed(project, int(version), device, thread)
        except ValueError as error:
            parser.exit(1, f'{error}\n')
    else:
        if not args.devices:
            parser.error('启动服务时需要 --devices')
        devices = json.loads(args.devices.read_text(encoding='utf-8-sig'))
        uvicorn.run(create_app(args.database, devices), host=args.host, port=args.port)
