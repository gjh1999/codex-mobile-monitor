"""Verify a resumable 64 MiB LFS transfer across handoff scans."""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import lfs_assets
from handoff import file_hash, git, prepare_resume
from tests.test_handoff_lfs import HandoffLFSTests
from tests.test_lfs_assets import ASSET


def measure(run):
    start = time.perf_counter()
    result = run()
    return result, round(time.perf_counter() - start, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    test = HandoffLFSTests()
    test.media_size = 64 * 1024 * 1024
    try:
        test.setUp()
        _, sealed = measure(test.auto.ship)
        test.enable_prefetch()
        test.media.interrupt = 16 * 1024 * 1024
        stalled, first = measure(test.scan)
        assert stalled['autoApply']['status'] == 'lfs_pending', stalled
        assert stalled['received'][0]['lfs'][ASSET]['status'] == 'interrupted', stalled
        test.media.interrupt = 0
        switched, resumed = measure(test.scan)
        assert switched['autoApply']['status'] == 'applied', switched
        assert test.media.requests == [(None, 16 * 1024 * 1024), ('bytes=16777216-', 48 * 1024 * 1024)]
        with patch('lfs_assets.file_hash', wraps=lfs_assets.file_hash) as hash_cache:
            repeated, repeat = measure(test.scan)
        assert repeated['autoApply']['status'] == 'applied' and hash_cache.call_count == 0, repeated
        fixture = test.fixture
        assert prepare_resume(fixture.seed, fixture.home_b)['thread'] == fixture.thread
        git(fixture.seed, 'lfs', 'install', '--local', '--skip-repo', isolated=True)
        test.media.missing = True
        _, checkout = measure(lambda: subprocess.run(
            ['git', 'lfs', 'checkout', '--', ASSET], cwd=fixture.seed, check=True, capture_output=True,
            env=dict(os.environ, GIT_LFS_SKIP_SMUDGE='0'), timeout=30))
        with (fixture.seed / ASSET).open('rb') as materialized:
            assert file_hash(materialized) == test.media.oid
        assert len(test.media.requests) == 2
        report = dict(objectBytes=test.media_size, sealAndShipSeconds=sealed, firstScanSeconds=first,
                      resumeAndApplySeconds=resumed, warmScanSeconds=repeat, warmAssetHashes=hash_cache.call_count,
                      checkoutSeconds=checkout, requests=test.media.requests,
                      downloadedBytes=sum(size for _, size in test.media.requests))
        (args.output / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
    finally:
        test.doCleanups()


if __name__ == '__main__':
    main()
