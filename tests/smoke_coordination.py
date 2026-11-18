"""Exercise the coordinator and two real Codex app-server connections."""
import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import Approvals
from coordination_client import ControlClient, ControlDenied
from coordination_server import create_app
from rpc_errors import RPCError
from universal_rpc import CodexRPC


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    fixture, output = options.fixture.resolve(), options.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    home = output / 'fixture-home'
    home.mkdir()
    shutil.copytree(fixture / 'cli-b/sessions', home / 'sessions')
    shutil.copyfile(fixture / 'cli-b/config.toml', home / 'config.toml')
    thread = json.loads((fixture / 'result.json').read_text())['thread']
    cwd = fixture / 'restored'
    project = 'fixture'
    devices = {name: dict(token=secrets.token_urlsafe(32), projects=[project]) for name in ('A', 'B', 'C')}
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    address, port = sock.getsockname()
    app = create_app(output / 'ownership.sqlite3', devices)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    worker.start()
    clients = {}
    rpcs = []
    try:
        start = time.monotonic()
        for name, entry in devices.items():
            config = dict(url=f'http://{address}:{port}', device=name,
                          projects={project: {'path': str(cwd), 'threads': [thread]}})
            clients[name] = ControlClient(config, entry['token'])
        a, b, c = (clients[name] for name in devices)
        while not server.started:
            if time.monotonic() - start > 5 or not worker.is_alive():
                raise RuntimeError('coordinator failed to listen')
            time.sleep(0.05)

        def launch(client=None):
            exe = shutil.which('codex.exe') or shutil.which('codex')
            process = subprocess.Popen([exe, 'app-server', '--stdio'], cwd=cwd,
                                       env=dict(os.environ, CODEX_HOME=str(home)),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, encoding='utf-8')
            rpc = CodexRPC(Approvals(), custom_process=process, coordination=client)
            rpcs.append(rpc)
            return rpc

        params = {'threadId': thread, 'excludeTurns': True}
        foreground = {**params, 'cwd': str(cwd)}
        first, human = launch(a), launch()
        initial = first.call('thread/resume', params)['thread']
        assert initial['status']['type'] == 'idle' and Path(initial['cwd']).resolve() == cwd
        try:
            human.call('thread/resume', foreground)
        except RPCError as error:
            assert 'active writer' in error.detail
        else:
            raise AssertionError('the original native connection failed to hold its write lock')

        b.event(project, 'vscode-window', 'vscode', 1, 'open')
        c.event(project, 'terminal', 'cli', 1, 'open')
        try:
            first.call('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': 'fixture'}]})
        except ControlDenied:
            pass
        else:
            raise AssertionError('the old worker sent a request after foreground preemption')
        assert first.reconcile_writes() and first.p.returncode == 0
        assert c.request(project, 'control')['writer'] is None
        assert human.call('thread/resume', foreground)['thread']['id'] == thread
        b.event(project, 'vscode-window', 'vscode', 2, 'close')
        assert len(a.request(project, 'control')['humans']) == 1
        human.p.stdin.close()
        human.p.wait(timeout=4)
        c.event(project, 'terminal', 'cli', 2, 'close')
        restarted = launch(a)
        assert restarted.call('thread/resume', params)['thread']['id'] == thread
        resumed = a.request(project, 'control')
        assert resumed['writer']['device'] == 'A'
        assert restarted.reconcile_writes(shutdown=True)
        report = dict(thread=thread, devices=list(devices), native_writer_transfer=True,
                      rejected_after_preemption=True, epoch_after_reacquire=resumed['version'],
                      elapsed_seconds=round(time.monotonic() - start, 3))
        (output / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
    finally:
        for rpc in rpcs:
            rpc.close()
            rpc.p.wait(timeout=4)
            rpc.p.stdin.close()
            rpc.p.stdout.close()
        server.should_exit = True
        worker.join(timeout=6)
        sock.close()


if __name__ == '__main__':
    main()
