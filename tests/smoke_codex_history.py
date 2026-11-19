"""Verify a real CLI → desktop App Server → CLI round trip with synthetic replies."""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import monitor
from automation import Approvals
from codex_history import CodexHistory
from conversation import read_conversation
from handoff import git
from reply_log import ReplyLog
from tests.smoke_handoff import Replies, native_turn
from tests.test_codex_history import THREAD, line, message
from universal_rpc import CodexRPC


def desktop_turn(exe, home, repo, tid, journal):
    env = dict(os.environ, CODEX_HOME=str(home), NO_PROXY='localhost,127.0.0.1')
    process = subprocess.Popen([exe, 'app-server', '--stdio'], cwd=repo, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
    try:
        rpc = CodexRPC(Approvals(), journal=journal, custom_process=process)
        try:
            resumed = rpc.call('thread/resume', {'threadId': tid, 'cwd': str(repo), 'excludeTurns': True})
            assert resumed['thread']['id'] == tid
            rpc.owned_threads.add(tid)
            turn = rpc.call('turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text': 'Desktop fixture B'}],
                                           'sandboxPolicy': {'type': 'readOnly'}, 'approvalPolicy': 'never'})['turn']['id']
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                rows = rpc.call('thread/turns/list', {'threadId': tid, 'limit': 1, 'sortDirection': 'desc'})['data']
                if any(row['id'] == turn and row['status'] == 'completed' for row in rows):
                    return
                time.sleep(0.05)
            raise TimeoutError('desktop fixture turn never completed')
        finally:
            rpc.close()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def roundtrip(directory):
    exe = shutil.which('codex.exe') or shutil.which('codex')
    assert exe, 'Codex CLI not available'
    repo, home = directory / 'workspace', directory / 'codex'
    repo.mkdir()
    git(repo, 'init', '--quiet')
    journal = ReplyLog(directory / 'replies.sqlite3')
    with ThreadingHTTPServer(('127.0.0.1', 0), Replies) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, CODEX_HOME=str(home)), patch.object(monitor, 'ROOT', directory):
                tid = native_turn(exe, home, repo, server.server_port)
                expected = ['Handoff fixture A', 'reply-on-A']
                assert [m['text'] for m in monitor.conversation_for(tid)['messages']] == expected
                desktop_turn(exe, home, repo, tid, journal)
                expected += ['Desktop fixture B', 'reply-on-B']
                assert [m['text'] for m in monitor.conversation_for(tid)['messages']] == expected
                local = read_conversation(directory / 'replies.sqlite3', tid)
                assert [m['text'] for m in local['messages']] == expected[2:]
                assert native_turn(exe, home, repo, server.server_port, tid, 'Terminal fixture C') == tid
                expected += ['Terminal fixture C', 'reply-on-return']
                assert [m['text'] for m in monitor.conversation_for(tid)['messages']] == expected
        finally:
            server.shutdown()
            thread.join(5)
            journal.db.close()
    return {'visible_messages': len(expected), 'unified_rounds': 3, 'native_cli': exe}


def benchmark(home):
    rollout = home / 'sessions' / '2026' / '11' / '19' / f'rollout-2026-11-19T09-00-00-{THREAD}.jsonl'
    rollout.parent.mkdir(parents=True)
    tool = line('response_item', {'type': 'function_call_output', 'output': 'x' * (512 * 1024)})
    with rollout.open('wb') as stream:
        stream.write(line('session_meta', {'id': THREAD}))
        stream.write(tool * 64)
        for index in range(50):
            stream.write(message('initial', 'assistant', f'fixture message {index}', f'item-{index}'))
    size = rollout.stat().st_size
    timings = {'cold_ms': [], 'unchanged_ms': [], 'append_ms': []}
    for index in range(5):
        reader = CodexHistory(home)
        started = time.perf_counter()
        assert len(reader.read(THREAD)[0]) == 50 + index
        timings['cold_ms'].append(round((time.perf_counter() - started) * 1000, 3))
        started = time.perf_counter()
        reader.read(THREAD)
        timings['unchanged_ms'].append(round((time.perf_counter() - started) * 1000, 3))
        with rollout.open('ab') as stream:
            stream.write(message('append', 'assistant', f'append {index}', f'new-{index}', 1))
        started = time.perf_counter()
        assert reader.read(THREAD)[0][-1]['text'] == f'append {index}'
        timings['append_ms'].append(round((time.perf_counter() - started) * 1000, 3))
    return {'bytes': size, 'timings': timings, 'medians': {key: statistics.median(values) for key, values in timings.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = {'roundtrip': roundtrip(root), 'reader': benchmark(root / 'benchmark')}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
