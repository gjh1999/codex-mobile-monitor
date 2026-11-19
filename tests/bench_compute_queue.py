"""Measure coordinator dispatch with a mix of queued and completed jobs."""
import argparse
import json
import sqlite3
import statistics
import tempfile
import time
import uuid
from pathlib import Path

from compute_queue import ComputeQueue
from coordination import Ownership


def measure(path, backlog, samples):
    ownership = Ownership(path, lambda: 100.)
    queue = ComputeQueue(ownership, {'vision': {'workers': {'office': 'local', 'home': 'home'}, 'profiles': {
        'cpu': {'cpu': 2, 'memoryMiB': 1024, 'vramMiB': 0},
        'gpu': {'cpu': 2, 'memoryMiB': 2048, 'vramMiB': 4096},
    }}})
    commit = '1' * 40
    ownership.report('vision', 'office', 'foreground', 'cli', 1, 'open')
    instance = str(uuid.uuid4())
    for device in ('office', 'home'):
        queue.heartbeat('vision', device, instance,
                        dict(profiles=['cpu', 'gpu'], commits=[commit], cpu=8, memoryMiB=8192, gpuVramMiB=[8192]))
    with ownership.transaction() as db:
        insert = '''INSERT INTO compute_jobs(project,id,submitter,session,profile,commit_id,created,state)
                    VALUES ('vision',?,'office','foreground','cpu',?,100,?)'''
        db.executemany(insert, ((f'fixture-{i}', commit, 'queued' if i % 2 == 0 else 'finished') for i in range(backlog)))
    timings, idle_timings = [], []
    for _ in range(samples):
        start = time.perf_counter()
        assert queue.claim('vision', 'home', instance) is None
        idle_timings.append((time.perf_counter() - start) * 1000)
        job_id = str(uuid.uuid4())
        queue.submit('vision', 'office', job_id, 'foreground', 'gpu', commit)
        start = time.perf_counter()
        job = queue.claim('vision', 'home', instance)
        timings.append((time.perf_counter() - start) * 1000)
        assert job['id'] == job_id and job['route'] == 'home_gpu', job
        queue.update('vision', 'home', job_id, instance, job['ticket'], 'start')
        queue.update('vision', 'home', job_id, instance, job['ticket'], 'finish', 0, '2' * 64)
    return dict(backlog=backlog, samples=samples, claimMedianMs=round(statistics.median(timings), 3),
                noClaimMedianMs=round(statistics.median(idle_timings), 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        data = dict(sqlite=sqlite3.sqlite_version, empty=measure(root / 'empty.db', 0, 30),
                    backlog=measure(root / 'backlog.db', 10000, 30))
    (args.output / 'result.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    print(json.dumps(data))


if __name__ == '__main__':
    main()
