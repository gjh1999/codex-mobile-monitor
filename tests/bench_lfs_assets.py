"""Measure LFS fetch, interrupted transfer recovery, and offline checkout."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from handoff import git
from tests.test_lfs_assets import ASSET, TWIN, LFSFixture


def measure(fixture, fetch=True):
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / 'lfs_assets.py'), '--repo', str(fixture.root),
           '--ref', fixture.commit, '--path', ASSET, '--path', TWIN]
    if fetch:
        cmd.extend(('--endpoint', fixture.endpoint, '--user-env', 'LANXI_LFS_FIXTURE_USER', '--token-env', 'LANXI_LFS_FIXTURE_TOKEN'))
    else:
        cmd.append('--check')
    env = dict(os.environ, LANXI_LFS_FIXTURE_USER='local-test', LANXI_LFS_FIXTURE_TOKEN='public-fixture-only')
    start = time.perf_counter()
    run = subprocess.run(cmd, capture_output=True, env=env, timeout=30)
    assert run.returncode in (0, 1), run.stderr.decode(errors='replace')
    result = json.loads(run.stdout)
    return dict(seconds=round(time.perf_counter() - start, 3), exitCode=run.returncode,
                status=result['status'], object=result['files'][ASSET])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    size, breakpoint = 64 * 1024 * 1024, 16 * 1024 * 1024
    with tempfile.TemporaryDirectory() as path:
        fresh = LFSFixture(Path(path), size)
        try:
            full = measure(fresh)
            assert full['status'] == 'ready' and fresh.requests == [(None, size)], full
        finally:
            fresh.close()
    with tempfile.TemporaryDirectory() as path:
        fixture = LFSFixture(Path(path), size)
        try:
            fixture.interrupt = breakpoint
            interrupted = measure(fixture)
            assert interrupted['exitCode'] == 1 and fixture.part.stat().st_size == breakpoint, interrupted
            fixture.interrupt = 0
            resumed = measure(fixture)
            assert resumed['status'] == 'ready', resumed
            assert fixture.requests == [(None, breakpoint), (f'bytes={breakpoint}-', size - breakpoint)]
            cached = measure(fixture, fetch=False)
            assert cached['status'] == 'ready' and len(fixture.batch_requests) == 2
            assert (fixture.root / ASSET).read_bytes() == fixture.pointer
        finally:
            fixture.close()
        git(fixture.root, 'lfs', 'install', '--local', '--skip-repo', isolated=True)
        start = time.perf_counter()
        subprocess.run(['git', 'lfs', 'checkout', '--', ASSET], cwd=fixture.root, check=True,
                       capture_output=True, env=dict(os.environ, GIT_LFS_SKIP_SMUDGE='0'), timeout=30)
        with (fixture.root / ASSET).open('rb') as materialized:
            assert hashlib.file_digest(materialized, 'sha256').hexdigest() == fixture.oid
        checkout = round(time.perf_counter() - start, 3)
        version = git(fixture.root, 'lfs', 'version', isolated=True).decode().strip()
    report = dict(size=size, breakpoint=breakpoint, gitLfs=version, fresh=full, interrupted=interrupted,
                  resumed=resumed, cached=cached, offlineCheckoutSeconds=checkout)
    (args.output / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
