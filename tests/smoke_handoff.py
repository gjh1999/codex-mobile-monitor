"""Run the installed native CLI twice with isolated homes and a localhost reply fixture."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from handoff import capture, find_session, git, prepare_resume, restore, session_info


class Replies(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if not self.path.endswith('/responses'):
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        previous = 'reply-on-A' in json.dumps(body['input'])
        text = 'reply-on-B' if previous else 'reply-on-A'
        item = dict(type='message', id='msg_fixture', status='completed', role='assistant',
                    content=[dict(type='output_text', text=text, annotations=[])])
        response = dict(id='resp_fixture', object='response', status='completed', output=[item],
                        usage=dict(input_tokens=1, output_tokens=1, total_tokens=2))
        events = [('response.created', dict(response=dict(id='resp_fixture', status='in_progress'))),
                  ('response.output_item.added', dict(output_index=0, item=dict(item, content=[]))),
                  ('response.output_text.delta', dict(item_id=item['id'], output_index=0, content_index=0, delta=text)),
                  ('response.output_item.done', dict(output_index=0, item=item)),
                  ('response.completed', dict(response=response))]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event, fields in events:
            self.wfile.write(f'event: {event}\ndata: {json.dumps(dict(type=event, **fields))}\n\n'.encode())


def native_turn(exe, home, cwd, port, thread=None):
    home.mkdir(exist_ok=True)
    config = f'''model = "gpt-5.4"
model_provider = "lanxi_fixture"
[model_providers.lanxi_fixture]
name = "Lanxi local fixture"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
stream_max_retries = 0
request_max_retries = 0
'''
    (home / 'config.toml').write_text(config, encoding='utf-8')
    env = dict(os.environ, CODEX_HOME=str(home), NO_PROXY='localhost,127.0.0.1')
    args = [exe, 'exec', '--json', '-s', 'read-only', '--cd', str(cwd)]
    args.extend(['resume', thread, 'Handoff fixture B'] if thread else ['Handoff fixture A'])
    p = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                       encoding='utf-8', errors='replace', timeout=90)
    if p.returncode:
        raise RuntimeError(f'Native CLI exit {p.returncode}: {p.stderr[-2000:]} {p.stdout[-2000:]}')
    events = [json.loads(line) for line in p.stdout.splitlines() if line.startswith('{')]
    sid = next(e['thread_id'] for e in events if e['type'] == 'thread.started')
    assert any(e['type'] == 'turn.completed' for e in events), events
    return sid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new verification directory')
    parser.add_argument('--source-parent', type=Path, default=Path(tempfile.gettempdir()))
    args = parser.parse_args()
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Replies)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(dir=args.source_parent) as scratch:
            a = Path(scratch) / 'source'
            a.mkdir()
            git(a, 'init', '--quiet', '-b', 'main')
            git(a, 'config', 'user.name', 'fixture')
            git(a, 'config', 'user.email', 'fixture@example.invalid')
            (a / 'code.bin').write_bytes(b'base\x00')
            git(a, 'add', '.')
            git(a, 'commit', '-qm', 'shared')
            seed = target / 'seed'
            git(target, 'clone', '--quiet', '--no-local', a, seed)
            base = git(a, 'rev-parse', 'HEAD').decode().strip()
            (a / 'local.txt').write_bytes(b'commit\n')
            git(a, 'add', '.')
            git(a, 'commit', '-qm', 'unpushed')
            (a / 'code.bin').write_bytes(b'staged\x00')
            git(a, 'add', '.')
            (a / 'code.bin').write_bytes(b'unstaged\x00')
            (a / 'new.txt').write_bytes(b'allowlisted\n')
            exe = shutil.which('codex.exe') or shutil.which('codex')
            home_a, home_b = a.parent / 'cli-a', target / 'cli-b'
            sid = native_turn(exe, home_a, a, server.server_port)
            package, b = target / 'handoff.zip', target / 'restored'
            receipt = capture(a, home_a, 'fixture', base, sid, ['new.txt'], package)
            restore(package, receipt['sha256'], 'fixture', seed, b)
            assert git(a, 'status', '--porcelain', '-z') == git(b, 'status', '--porcelain', '-z')
            assert git(b, 'show', ':code.bin') == b'staged\x00'
            assert (b / 'code.bin').read_bytes() == b'unstaged\x00'
            assert (b / 'new.txt').read_bytes() == b'allowlisted\n'
            binding = prepare_resume(b, home_b)
            assert native_turn(exe, home_b, Path(binding['cwd']), server.server_port, binding['thread']) == sid
            (log,) = find_session(home_b, sid)
            data = log.read_bytes()
            _, next_turn, _ = session_info(data)
            events = [json.loads(line) for line in data.splitlines()]
            contexts = [e['payload'] for e in events if e['type'] == 'turn_context']
            assert next_turn != receipt['turn'] and Path(contexts[-1]['cwd']).resolve() == b
            assert any(e['type'] == 'event_msg' and e['payload'].get('turn_id') == next_turn
                       and e['payload'].get('last_agent_message') == 'reply-on-B' for e in events)
            onward = capture(b, home_b, 'fixture', base, sid, ['new.txt'], target / 'onward.zip')
            assert onward['turn'] == next_turn
            report = dict(receipt, next_turn=next_turn, cwd_a=str(a), cwd_b=str(b), reply='reply-on-B',
                          codex=subprocess.check_output([exe, '--version'], text=True).strip(),
                          git=git(a, '--version').decode().strip(), backend='localhost fixture')
            (target / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
