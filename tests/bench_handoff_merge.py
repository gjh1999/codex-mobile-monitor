"""Measure sparse edits and conflicts with a repeatable, larger Git history."""
import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import handoff
from handoff import git
from handoff_merge import prepare_merge
from tests.test_handoff_merge import MergeTests


def measured(operation):
    counts = Counter()
    original = handoff.git_process

    def run(repo, *args, **kwargs):
        names = ('merge-tree', 'checkout-index', 'pack-objects')
        counts['git'] += 1
        for command in names:
            if command in args:
                counts[command] += 1
        return original(repo, *args, **kwargs)

    start = time.perf_counter()
    with patch('handoff.git_process', run), patch('handoff_merge.git_process', run):
        result = operation()
    return result, dict(seconds=round(time.perf_counter() - start, 3), commands=dict(counts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--files', type=int, default=768)
    parser.add_argument('--file-kib', type=int, default=64)
    parser.add_argument('--diverged', action='store_true', help='measure independent commits with the same shared baseline')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    fixture = MergeTests()
    fixture.setUp()
    try:
        stock = fixture.local / 'fixtures'
        stock.mkdir()
        for i in range(args.files):
            (stock / f'{i:06}.bin').write_bytes(hashlib.shake_128(f'lanxi fixture {i}'.encode()).digest(args.file_kib * 1024))
        git(fixture.local, 'add', 'fixtures')
        git(fixture.local, 'commit', '-qm', 'larger shared baseline')
        git(fixture.sender, 'pull', '--quiet', '--ff-only')
        baseline = 'HEAD'
        if args.diverged:
            baseline = git(fixture.local, 'rev-parse', 'HEAD').decode().strip()
            fixture.commit(fixture.local, 'left.txt', b'local commit\n')
            fixture.commit(fixture.sender, 'right.txt', b'incoming commit\n')
        fixture.edit(fixture.local, 2, b'local staged')
        git(fixture.local, 'add', 'shared.txt')
        fixture.edit(fixture.sender, 26, b'incoming working')
        _, transfer = measured(lambda: fixture.handoff(base=baseline))
        result, success = measured(fixture.merge)
        assert result['status'] == 'merged', result
        assert result['history']['mode'] == ('merge_commit' if args.diverged else 'same_head')
        assert success['commands']['checkout-index'] == success['commands']['pack-objects'] == 1
        assert b'incoming working' in (fixture.dest / 'shared.txt').read_bytes()
        fixture.edit(fixture.local, 18, b'left conflict')
        fixture.edit(fixture.incoming, 18, b'right conflict')
        rejected, conflict = measured(lambda: prepare_merge(fixture.local, fixture.incoming, fixture.root / 'conflict'))
        assert rejected['status'] == 'conflict', rejected
        assert 'checkout-index' not in conflict['commands'] and 'pack-objects' not in conflict['commands']
        data = dict(files=args.files, file_kib=args.file_kib, shared_mib=args.files * args.file_kib / 1024,
                    git=git(fixture.local, '--version').decode().strip(),
                    mode=result['history']['mode'], capture_and_restore=transfer, merge=success,
                    conflict=dict(conflict, layer=rejected['layer']))
        (args.output / 'result.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
        print(json.dumps(data))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
