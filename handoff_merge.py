"""Prepare an independent Git worktree from local and received handoff state."""
import argparse
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from handoff import (DIFF, GIT_ID, MAX_FILE, MAX_TOTAL, HandoffError, check_filters, checked_index, excluded_paths, git,
                     git_process, limited, local_path, names, require, resume_context, session_info, sha, switch_pending,
                     tracked_lfs, work_file)
from lfs_assets import copy_cache, inventory, require_cached


class MergeConflict(ValueError):
    def __init__(self, layer, paths, messages):
        super().__init__(layer)
        self.report = dict(version=1, status='conflict', layer=layer, paths=paths, messages=messages)


class SnapshotChanged(HandoffError):
    pass


class WorkspaceDirty(HandoffError):
    pass


def separate(a, b):
    return not a.is_relative_to(b) and not b.is_relative_to(a)


def worktree_root(path):
    root = path.resolve(strict=True)
    reported = git(root, 'rev-parse', '--show-toplevel', isolated=True).decode().strip()
    require(root == Path(reported).resolve(), '请指定 Git 工作区根目录')
    return root


@dataclass(frozen=True)
class InputState:
    head: str
    branch: str | None
    index: bytes
    changes: tuple
    stamps: tuple


def hash_changes(root, source_args, source_index, files, write_objects, tracked):
    if not files:
        return []
    flags = ['-w'] if write_objects else []
    paths = ''.join(path + '\n' for path, _ in files).encode('utf-8')
    oids = git(root, *source_args, 'hash-object', *flags, '--filters' if tracked else '--no-filters',
               '--stdin-paths', data=paths, isolated=True, index_file=source_index).decode().splitlines()
    require(len(oids) == len(files) and all(GIT_ID.fullmatch(oid) for oid in oids), '读取文件对象失败')
    return [(item, mode, oid) for (item, mode), oid in zip(files, oids)]


def capture_index(root, temporary, includes, write_objects, lfs_paths=(), lfs_memo=None):
    lfs = tracked_lfs(root, 'HEAD', lfs_paths, lfs_memo, optional=True)
    head, branch, index = checked_index(root, lfs)
    entries = {path: header[:6] for header, path in (line.split('\t', 1) for line in names(index))}
    require(all(mode in ('100644', '100755') for mode in entries.values()), '合并仅支持普通文件')

    # Read worktree content with the isolated repository's configuration, borrowing
    # the source index read-only. Neither user nor source filter commands can run.
    source_index = git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip()
    source_args = ['--git-dir=' + str(temporary / '.git'), '--work-tree=' + str(root)]
    settings = git(root, 'config', '--get-regexp', r'^core\.(autocrlf|filemode)$', optional=True) or b''
    for line in settings.decode().splitlines():
        key, value = line.split(' ', 1)
        value = value.strip().lower()
        valid = ('true', 'false', 'yes', 'no', 'on', 'off', '1', '0')
        require(value in valid or (key == 'core.autocrlf' and value == 'input'), 'Git 工作区设置无效')
        source_args.extend(('-c', f'{key}={value}'))
    dirty = set(names(git(root, *source_args, *DIFF, '--name-only', '-z', *excluded_paths(lfs), isolated=True,
                          index_file=source_index, literal=not lfs)))
    if includes:
        untracked = set(names(git(root, 'ls-files', '--others', '--exclude-standard', '-z')))
        require(set(includes) <= untracked, '纳入的新文件需为未忽略的单个未跟踪文件')
    tracked, new, deletions, stamps = [], [], [], []
    total = 0
    for item in sorted(dirty | set(includes)):
        leaf = local_path(root, item)
        try:
            info = leaf.stat()
        except FileNotFoundError:
            info = None
        if info is None or stat.S_ISDIR(info.st_mode):
            require(item in dirty, '要合并的新文件已不存在')
            deletions.append((item, '0', '0' * len(head)))
            continue
        require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE, '变更需为普通文件并符合单文件上限')
        total += info.st_size
        require(total <= MAX_TOTAL, '工作区变更超过容量上限')
        mode = (entries.get(item, '100644') if os.name == 'nt'
                else '100755' if info.st_mode & stat.S_IXUSR else '100644')
        stamps.append((item, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        (tracked if item in dirty else new).append((item, mode))

    changes = deletions + hash_changes(root, source_args, source_index, tracked, write_objects, tracked=True)
    changes += hash_changes(root, source_args, source_index, new, write_objects, tracked=False)
    return InputState(head, branch, index, tuple(changes), tuple(stamps))


def trees(repo, state):
    git(repo, 'read-tree', '--empty', isolated=True)
    if state.index:
        git(repo, 'update-index', '-z', '--index-info', data=state.index, isolated=True)
    index = git(repo, 'write-tree', isolated=True).decode().strip()
    if not state.changes:
        return index, index
    entries = ''.join(f'{mode} {oid}\t{path}\0' for path, mode, oid in state.changes).encode('utf-8')
    git(repo, 'update-index', '-z', '--index-info', data=entries, isolated=True)
    return index, git(repo, 'write-tree', isolated=True).decode().strip()


def merge_tree(repo, base, local, incoming, layer):
    if incoming == base or local == incoming:
        return local
    if local == base:
        return incoming
    base_arg = ['--merge-base=' + base] if base is not None else []
    result = git_process(repo, 'merge-tree', '--write-tree', '--name-only', '--messages', '-z',
                         *base_arg, local, incoming, isolated=True)
    fields = result.stdout.decode('utf-8', 'replace').split('\0')
    if result.returncode == 1:
        separator = fields.index('', 1)
        details = fields[separator + 1:]
        messages = []
        offset = 0
        while offset < len(details) and details[offset]:
            count = int(details[offset])
            end = offset + count + 1
            messages.append({'paths': details[offset + 1:end], 'type': details[end]})
            offset = end + 2
        raise MergeConflict(layer, fields[1:separator], messages)
    require(result.returncode == 0 and GIT_ID.fullmatch(fields[0]),
            'Git 三方合并失败，请检查本机 Git 是否支持 merge-tree --merge-base')
    return fields[0]


def history_base(repo, history, local_tree, incoming_tree):
    local, incoming = history['localHead'], history['incomingHead']
    if local == incoming:
        history.update(mode='same_head', bases=[local])
        return local_tree
    result = git_process(repo, 'merge-base', '--all', local, incoming, isolated=True)
    if result.returncode == 1 and not result.stdout:
        history['reason'] = 'unrelated_history'
        return None
    bases = result.stdout.decode().splitlines()
    require(result.returncode == 0 and bases and all(GIT_ID.fullmatch(base) for base in bases),
            '无法确定两端 Git 提交的共同祖先')
    history['bases'] = bases
    if bases == [incoming]:
        history['mode'] = 'local_ahead'
        return local_tree
    if bases == [local]:
        history['mode'] = 'incoming_ahead'
        return incoming_tree
    history['mode'] = 'merge_commit'
    return merge_tree(repo, None, local, incoming, 'history')


def check_history_paths(repo, committed, previous):
    raw = git(repo, 'diff-tree', '-r', '--no-renames', '--diff-filter=ACMT', '--raw', '--no-abbrev', '-z',
              previous, committed, isolated=True)
    entries = names(raw)
    for header, path in zip(entries[::2], entries[1::2]):
        require(header.split()[1] in ('100644', '100755'), '合并后的提交仅支持普通文件')
        local_path(repo, path)


def merged_head(repo, history, base):
    local, incoming = history['localHead'], history['incomingHead']
    if history['mode'] == 'incoming_ahead':
        return incoming
    if history['mode'] != 'merge_commit':
        return local
    return git(repo, '-c', 'user.name=Lanxi Workbench', '-c', 'user.email=lanxi@localhost',
               'commit-tree', base, '-p', local, '-p', incoming,
               '-m', 'Merge workspaces for Lanxi handoff', isolated=True).decode().strip()


def object_store(repo):
    path = git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'objects',
               isolated=True).decode().strip()
    require('\n' not in path and '\r' not in path and Path(path).is_dir(), 'Git 对象目录不可用')
    return path


def check_checkout_paths(repo, target_repo=None, lfs=()):
    entries = names(git(repo, 'ls-files', '--stage', '-z', isolated=True))
    paths, spellings = [], {}
    for entry in entries:
        header, path = entry.split('\t', 1)
        require(header[:6] in ('100644', '100755'), '合并仅支持普通文件')
        paths.append(path)
        # Check directory names as well as files for Windows case collisions.
        while path:
            key = path.casefold()
            previous = spellings.get(key)
            require(previous is None or previous == path, '合并结果存在大小写路径冲突')
            if previous is not None:
                break
            spellings[key] = path
            path = path.rpartition('/')[0]
    paths = ''.join(path + '\0' for path in paths).encode('utf-8')
    if target_repo is not None:
        filters = git(target_repo, 'check-attr', '--cached', '-z', '--stdin', 'filter', data=paths,
                      index_file=repo / '.git/index')
    else:
        filters = git(repo, 'check-attr', '--cached', '-z', '--stdin', 'filter', data=paths, isolated=True)
    check_filters(names(filters), lfs)


def verify_snapshot(root, state, manifest):
    files = {entry['path']: entry for entry in manifest['files']}
    if sha(state.index) != manifest['index_hash'] or {row[0] for row in state.changes} != files.keys():
        raise SnapshotChanged('预备工作区的暂存区或文件清单已变化')
    for name, entry in files.items():
        raw, executable = work_file(root, name)
        expected = manifest['objects'].get(entry['object'])
        if (sha(raw) if raw is not None else None) != expected or (os.name != 'nt' and executable != entry['executable']):
            raise SnapshotChanged('预备工作区内容已偏离签名包')


def prepare_merge(local, incoming, dest, includes=(), incoming_includes=(), *, verified=None):
    local, incoming = worktree_root(local), worktree_root(incoming)
    dest = dest.absolute()
    require(all(separate(a, b) for a, b in ((local, incoming), (local, dest.resolve()), (incoming, dest.resolve()))),
            '合并预备目录必须独立于两个输入工作区')
    require(dest.parent.is_dir() and not dest.exists() and not dest.is_symlink(), '请指定尚不存在的预备目录')
    require(not switch_pending(local), '原工作区有尚未结束的切换')

    local_head = git(local, 'rev-parse', '--verify', 'HEAD^{commit}', isolated=True).decode().strip()
    incoming_head = git(incoming, 'rev-parse', '--verify', 'HEAD^{commit}', isolated=True).decode().strip()
    context = resume_context(incoming)
    require(context is not None, '输入缺少已恢复的交接回执')
    receipt = json.loads(limited(context.private / 'handoff.json', 65536))
    require(receipt['head'] == incoming_head, '预备目录已切换到与交接回执不同的提交')
    lfs = inventory(receipt)
    if lfs:
        require_cached(incoming, lfs)
    source_options = dict(lfs_paths=lfs, lfs_memo={}) if lfs else {}
    transcript = limited(context.private / 'session.jsonl')
    require(sha(transcript) == context.checksum and session_info(transcript)[:2] == (context.thread, context.turn),
            '交接历史与回执不匹配')
    registered = receipt.get('included', [])
    require(isinstance(registered, list) and all(isinstance(path, str) for path in registered), '交接新文件回执无效')
    if verified and (any(receipt[key] != verified[key] for key in ('project', 'head', 'thread', 'turn', 'cwd', 'rollout'))
                     or inventory(verified) != lfs or context.checksum != verified['objects']['session.jsonl']
                     or set(registered) != {row['path'] for row in verified['files'] if row['new']}):
        raise SnapshotChanged('预备工作区回执与签名包不符')
    remote_paths = sorted(set(registered) | set(incoming_includes))
    history = dict(localHead=local_head, incomingHead=incoming_head)
    unavailable = dict(version=1, status='history_unavailable', history=history)
    if len(local_head) != len(incoming_head):
        history['reason'] = 'object_format'
        return unavailable
    if local_head != incoming_head and any(
            git(root, 'rev-parse', '--is-shallow-repository', isolated=True).strip() == b'true'
            for root in (local, incoming)):
        history['reason'] = 'shallow_history'
        return unavailable

    with tempfile.TemporaryDirectory(dir=dest.parent, prefix='.lanxi-merge-') as directory:
        work = Path(directory) / 'work'
        work.mkdir()
        format_name = 'sha256' if len(local_head) == 64 else 'sha1'
        git(work, 'init', '--quiet', '--template=', '--object-format=' + format_name, isolated=True)
        alternates = work / '.git' / 'objects' / 'info' / 'alternates'
        paths = dict.fromkeys((object_store(local), object_store(incoming)))
        alternates.write_bytes(''.join(path + '\n' for path in paths).encode('utf-8'))
        revisions = [commit + '^{tree}' for commit in (local_head, incoming_head)]
        local_tree, incoming_tree = git(work, 'rev-parse', *revisions, isolated=True).decode().splitlines()
        try:
            base = history_base(work, history, local_tree, incoming_tree)
            if base is None:
                return unavailable
            for previous in dict.fromkeys((local_tree, incoming_tree)):
                if base != previous:
                    check_history_paths(work, base, previous)

            here = capture_index(local, work, includes, write_objects=True, **source_options)
            there = capture_index(incoming, work, remote_paths, write_objects=True, **source_options)
            require((here.head, there.head) == (local_head, incoming_head), '输入提交在读取期间发生变化')
            if verified:
                verify_snapshot(incoming, there, verified)
            local_index, local_work = trees(work, here)
            if verified and (local_index != local_tree or local_work != local_tree):
                raise WorkspaceDirty('原工作区在自动预备期间有本机修改')
            remote_index, remote_work = trees(work, there)
            local_staged = merge_tree(work, local_tree, base, local_index, 'local_index')
            incoming_staged = merge_tree(work, incoming_tree, base, remote_index, 'incoming_index')
            index = merge_tree(work, base, local_staged, incoming_staged, 'index')
            left = merge_tree(work, local_index, index, local_work, 'local_worktree')
            right = merge_tree(work, remote_index, index, remote_work, 'incoming_worktree')
            working = merge_tree(work, index, left, right, 'worktree')
        except MergeConflict as error:
            error.report['history'] = history
            return error.report

        head = merged_head(work, history, base)
        git(work, 'read-tree', working, isolated=True)
        check_checkout_paths(work, lfs=lfs)
        git(work, 'checkout-index', '--all', isolated=True)
        git(work, *DIFF, '--quiet', isolated=True)
        if index != working:
            git(work, 'read-tree', index, isolated=True)
            check_checkout_paths(work, lfs=lfs)
        if here.branch:
            git(work, 'symbolic-ref', 'HEAD', 'refs/heads/' + here.branch, isolated=True)
            git(work, 'update-ref', 'HEAD', head, isolated=True)
        else:
            git(work, 'update-ref', '--no-deref', 'HEAD', head, isolated=True)
        if lfs:
            require(tracked_lfs(work, head, lfs) == lfs, '合并后的 LFS 指针与接收清单不一致')
            for tree in dict.fromkeys((index, working)):
                require(not git(work, 'diff-tree', '-r', '--name-only', head + '^{tree}', tree, '--', *lfs,
                                isolated=True), '合并结果含未提交的 LFS 修改')
            copy_cache(incoming, work, lfs)

        # Materialize only objects reachable from the final worktree, index and
        # HEAD in a single pack. Drop the borrowing link before publishing.
        roots = f'{head}\n{index}\n{working}\n'.encode('ascii')
        prefix = work / '.git' / 'objects' / 'pack' / 'pack'
        git(work, '-c', 'pack.compression=1', '-c', 'pack.threads=2', 'pack-objects', '--quiet', '--revs', '--delta-base-offset',
            prefix, data=roots, isolated=True)
        require(capture_index(local, work, includes, write_objects=False, **source_options) == here
                and capture_index(incoming, work, remote_paths, write_objects=False, **source_options) == there
                and not (switch_pending(local) or switch_pending(incoming)),
                '输入工作区在合并期间发生变化')
        if verified:
            verify_snapshot(incoming, there, verified)
        alternates.unlink()
        git(work, 'fsck', '--connectivity-only', '--no-dangling', head, index, working, isolated=True)
        relative = context.cwd.relative_to(incoming)
        require((work / relative).is_dir(), '合并后缺少会话工作目录')
        private = work / '.git' / 'lanxi'
        private.mkdir()
        (private / 'session.jsonl').write_bytes(transcript)
        result = dict(version=1, status='merged', head=head, indexTree=index, worktreeTree=working, cwd=str(dest / relative),
                      project=context.project, thread=context.thread, history=history)
        untracked = names(git(work, 'ls-files', '--others', '--exclude-standard', '-z', isolated=True))
        receipt['included'] = sorted(set(untracked) & (set(includes) | set(remote_paths)))
        receipt['head'] = head
        receipt['merge'] = {key: result[key] for key in ('indexTree', 'worktreeTree', 'history')}
        (private / 'handoff.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        origin = git(local, 'config', '--local', '--get', 'remote.origin.url', optional=True, isolated=True)
        if origin:
            git(work, 'remote', 'add', '--', 'origin', origin.decode().strip(), isolated=True)
        require(not dest.exists() and not dest.is_symlink(), '预备目录已被占用')
        work.rename(dest)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('local', 'incoming', 'dest'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--include', action='append', default=[], help='纳入合并的本机新文件')
    parser.add_argument('--include-incoming', action='append', default=[], help='旧格式回执需明确纳入的新文件')
    args = parser.parse_args()
    try:
        result = prepare_merge(args.local, args.incoming, args.dest, args.include, args.include_incoming)
    except (HandoffError, OSError, ValueError, TypeError, KeyError, IndexError) as error:
        parser.exit(2, f'工作区合并失败：{error}\n')
    print(json.dumps(result))
    return 0 if result['status'] == 'merged' else 1


if __name__ == '__main__':
    raise SystemExit(main())
