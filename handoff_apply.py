"""Apply an already merged handoff to an idle, registered Windows worktree."""
import argparse
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from coordination_client import ControlDenied, device_client
from handoff import (GIT_ID, MAX_FILE, HandoffError, checked_index, excluded_paths, file_hash, git, limited, local_path,
                     names, native_publication_lock, require, session_info, session_progress, sha, tracked_lfs)
from handoff_merge import WorkspaceDirty, check_checkout_paths, object_store, separate, worktree_root
from handoff_sync import absolute, atomic_write, encode, identifier, local_home
from lfs_assets import copy_cache, inventory, require_cached
from service_lock import ServiceLock


RECEIPT_FILES = ('.git/lanxi/session.jsonl', '.git/lanxi/handoff.json')
FAILURES = (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RuntimeError, ControlDenied)


def entry(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT, '切换路径包含链接或重解析点')
    return info


def digest(path):
    info = entry(path)
    if info is None:
        return None
    require(stat.S_ISREG(info.st_mode), '切换目标已被目录或特殊文件占用')
    with path.open('rb') as stream:
        return file_hash(stream)


def target(root, name):
    if name in RECEIPT_FILES:
        entry(root / '.git/lanxi')
        return root / name
    return local_path(root, name)


def primary_repo(root):
    require(worktree_root(root) == root, '切换需要原工作区根目录')
    gitdir = root / '.git'
    require(entry(gitdir) and gitdir.is_dir() and Path(git(root, 'rev-parse', '--absolute-git-dir',
                                                         isolated=True).decode().strip()) == gitdir,
            '切换只支持独立 Git 工作区')
    return gitdir


def checkout_settings(root):
    settings = []
    for key in ('core.autocrlf', 'core.eol', 'core.filemode'):
        value = git(root, 'config', '--get', key, optional=True)
        if value is not None:
            settings.extend(('-c', f'{key}={value.decode().strip()}'))
    return settings


def clean(root, lfs_paths=(), lfs_memo=None):
    lfs = tracked_lfs(root, 'HEAD', lfs_paths, lfs_memo, optional=True)
    head, branch, _ = checked_index(root, lfs)
    require(branch, '请先切换到本机 Git 分支')
    if git(root, *checkout_settings(root), 'status', '--porcelain=v1', '-z', '--untracked-files=no',
           *excluded_paths(lfs), isolated=True, literal=not lfs):
        raise WorkspaceDirty('原工作区存在已暂存或未暂存的本机修改')
    return head, 'refs/heads/' + branch


def delta(repo, old, new):
    raw = names(git(repo, 'diff-tree', '-r', '--raw', '--no-abbrev', '--no-renames', '-z', old, new, isolated=True))
    changes = {}
    for header, name in zip(raw[::2], raw[1::2]):
        before, after = header[1:].split()[:2]
        require({before, after} <= {'000000', '100644', '100755'}, '只支持变更普通文件')
        changes[name] = before != '000000', after != '000000'
    return changes


def import_objects(root, gitdir, source_git):
    # prepare_merge publishes one self-contained pack. Build its index through
    # Git and stream from disk without holding the pack in a Python buffer.
    pack_dir = source_git / 'objects/pack'
    require(entry(source_git / 'objects') and entry(pack_dir), '预备工作区缺少 Git 对象目录')
    packs = [p for p in pack_dir.iterdir() if re.fullmatch(r'pack-(?:[0-9a-f]{40}|[0-9a-f]{64})\.pack', p.name)]
    require(len(packs) == 1 and entry(packs[0]) and packs[0].is_file(), '需要刚生成的独立合并包')
    require(entry(gitdir / 'objects') and entry(gitdir / 'objects/pack'), '原仓库对象目录不可用')
    with packs[0].open('rb') as stream:
        git(root, 'index-pack', '--stdin', stream=stream, isolated=True)


def prepared_receipt(prepared):
    source_git = primary_repo(prepared)
    require(entry(source_git / 'lanxi') and (source_git / 'lanxi').is_dir(), '缺少交接回执目录')
    require(entry(source_git / 'lanxi/handoff.json') and entry(source_git / 'lanxi/session.jsonl'), '缺少交接回执')
    marker = json.loads(limited(source_git / 'lanxi/handoff.json', 65536))
    return source_git, marker


def prepare(root, gitdir, prepared, source_git, marker, tx, project, home, before, ref, automatic=None, lfs_memo=None):
    lfs = inventory(marker)
    merge = marker['merge']
    head, index, working = marker['head'], merge['indexTree'], merge['worktreeTree']
    require(marker['version'] == 1 and marker['project'] == project and merge['history']['localHead'] == before
            and all(GIT_ID.fullmatch(oid) and len(oid) == len(before) for oid in (head, index, working)),
            '合并回执与登记项目或当前提交不匹配')
    require(git(prepared, 'rev-parse', '--verify', 'HEAD', isolated=True).decode().strip() == head,
            '预备工作区已离开合并提交')
    git(prepared, 'merge-base', '--is-ancestor', before, head, isolated=True)
    if lfs:
        require(tracked_lfs(prepared, head, lfs) == lfs, '合并后的 LFS 指针与回执不一致')
        for tree in dict.fromkeys((index, working)):
            require(not git(prepared, 'diff-tree', '-r', '--name-only', head + '^{tree}', tree, '--', *lfs,
                            isolated=True), '预备目录含未提交的 LFS 修改')
        copy_cache(prepared, root, lfs)
    transcript = limited(source_git / 'lanxi/session.jsonl')
    thread, turn, _ = session_info(transcript)
    require((thread, turn, sha(transcript)) == (marker['thread'], marker['turn'], marker['sha256']), '会话回执校验失败')
    home.mkdir(parents=True, exist_ok=True)
    session_progress(home, thread, transcript)
    if automatic:
        require((marker['package_sha256'], thread, turn) == (automatic['sha256'], automatic['thread'], automatic['turn'])
                and ref == 'refs/heads/' + automatic['branch'] and inventory(automatic) == lfs,
                '自动切换来源或本机分支已变化')
        marker['automatic'] = automatic

    old_tree = before + '^{tree}'
    work_changes = delta(prepared, old_tree, working)
    index_changes = delta(prepared, old_tree, index)
    for name, (exists, _) in {**index_changes, **work_changes}.items():
        path = target(root, name)
        info = entry(path)
        require((info is not None) == exists and (not info or stat.S_ISREG(info.st_mode)),
                f'本机路径与预备变更冲突：{name}')
        require(all(not p.exists() or p.is_dir() for p in path.parents if p.is_relative_to(root)),
                f'不支持文件与目录互换：{name}')

    import_objects(root, gitdir, source_git)
    git(root, 'fsck', '--connectivity-only', '--no-dangling', head, index, working, isolated=True)
    stage = tx / 'stage'
    stage.mkdir()
    format_name = 'sha256' if len(head) == 64 else 'sha1'
    git(stage, 'init', '--quiet', '--template=', '--object-format=' + format_name, isolated=True)
    alternates = stage / '.git/objects/info/alternates'
    alternates.write_bytes((object_store(root) + '\n').encode())
    git(stage, 'read-tree', working, isolated=True)
    check_checkout_paths(stage, root, lfs)
    changed = ''.join(name + '\0' for name, (_, present) in work_changes.items() if present)
    if changed:
        git(stage, *checkout_settings(root), 'checkout-index', '-z', '--stdin', data=changed.encode(), isolated=True)
    if index != working:
        git(stage, 'read-tree', index, isolated=True)
        check_checkout_paths(stage, root, lfs)
    if marker['cwd'] != '.':
        cwd = local_path(root, marker['cwd']).relative_to(root).as_posix() + '/'
        paths = git(prepared, 'ls-tree', '-r', '-z', working, '--', cwd, isolated=True)
        require(paths, '合并工作区缺少会话工作目录')
    public = stage / '.git/lanxi'
    public.mkdir()
    (public / 'session.jsonl').write_bytes(transcript)
    (public / 'handoff.json').write_bytes(encode(dict(marker, applied=True)))
    source_index = gitdir / 'index'
    require(digest(source_index) is not None, '原工作区缺少 Git 索引')
    shutil.copyfile(source_index, tx / 'old-index')
    changes = []
    directories = set()
    for name in (*work_changes, *RECEIPT_FILES):
        path = target(root, name)
        for parent in path.parents:
            if parent == root or parent.exists():
                break
            directories.add(parent.relative_to(root).as_posix())
        changes.append(dict(path=name, old=digest(path), new=digest(stage / name)))

    require(clean(root, lfs, lfs_memo) == (before, ref), '原工作区在预备期间发生变化')
    return dict(before=before, head=head, ref=ref, thread=thread, turn=turn, cwd=marker['cwd'],
                changes=changes, directories=sorted(directories, key=lambda path: (path.count('/'), path)))


def install_files(root, tx, plan):
    for row in plan['changes']:
        name = row['path']
        path, stage, backup = target(root, name), tx / 'stage' / name, tx / 'backup' / name
        info = entry(path)
        require(digest(stage) == row['new'] and (info is not None) == (row['old'] is not None)
                and (not info or stat.S_ISREG(info.st_mode)), f'文件在切换期间发生变化：{name}')
        if row['old'] is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            path.rename(backup)
            if digest(backup) != row['old']:
                backup.rename(path)
                raise HandoffError(f'文件在切换期间发生变化：{name}')
        if row['new'] is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            stage.rename(path)


def restore_files(root, tx, plan):
    for row in reversed(plan['changes']):
        name = row['path']
        path, stage, backup = target(root, name), tx / 'stage' / name, tx / 'backup' / name
        if row['new'] is not None and not stage.exists():
            require(digest(path) == row['new'], f'已切换文件随后发生修改，已保留现场：{name}')
            path.rename(stage)
        if backup.exists():
            require(digest(path) is None and digest(backup) == row['old'], f'回滚路径或备份已变化：{name}')
            backup.rename(path)
    for name in reversed(plan['directories']):
        folder = root / '.git/lanxi' if name == '.git/lanxi' else local_path(root, name)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def current_branch(root):
    return git(root, 'symbolic-ref', 'HEAD', isolated=True).decode().strip()


def move_head(root, ref, before, after):
    require(current_branch(root) == ref, '当前分支已变动，已保留切换现场')
    if before != after:
        # The held index lock excludes ordinary Git checkout. Update the
        # specific branch only if its old object ID still matches.
        git(root, 'update-ref', '--no-deref', '-m', 'Lanxi workspace handoff', ref, after, before, isolated=True)


def undo(root, gitdir, tx, plan):
    require(current_branch(root) == plan['ref'],
            '当前分支已变动，已保留切换现场')
    head = git(root, 'rev-parse', '--verify', 'HEAD', isolated=True).decode().strip()
    require(head in (plan['before'], plan['head']), '当前提交已变动，已保留切换现场')
    index, lock = gitdir / 'index', gitdir / 'index.lock'
    stage, original = tx / 'stage/.git/index', tx / 'old-index'
    current = digest(index)
    require(current in (digest(stage), digest(original)), 'Git 索引已变动，已保留切换现场')
    if entry(lock):
        require(lock.samefile(stage) or lock.samefile(original), 'Git 正在操作索引，已保留切换现场')
    else:
        os.link(original, lock)
    restore_files(root, tx, plan)
    move_head(root, plan['ref'], head, plan['before'])
    if current != digest(original):
        if not lock.samefile(original):
            lock.unlink()
            os.link(original, lock)
        lock.replace(index)
    else:
        lock.unlink()


def switch(client, project, prepared, home, *, automatic=None, revalidate=None):
    require(os.name == 'nt', '原路径切换当前支持 Windows')
    root = next(path for path, name in client.paths.items() if name == project)
    require(prepared is None or separate(prepared, root), '预备目录必须独立于原工作区')
    gitdir = primary_repo(root)
    directory = gitdir / 'lanxi-switch'
    entry(directory)
    directory.mkdir(exist_ok=True)
    active = directory / 'active.json'
    lock_path = directory / 'switch.lockfile'
    entry(lock_path)
    lock = ServiceLock(lock_path)
    try:
        if prepared is None:
            if not entry(active):
                return dict(version=1, status='no_pending_switch', project=project)
            record = json.loads(limited(active))
            require(record['version'] == 1 and record['project'] == project
                    and record['phase'] in ('reserved', 'applying', 'applied', 'rolled_back'),
                    '切换日志与登记项目不匹配')
        else:
            require(not entry(active), '已有未完成的切换，请先执行 --recover')
            record = dict(version=1, project=project, session=str(uuid.uuid4()), phase='reserved')
            atomic_write(active, encode(record))
        session = str(uuid.UUID(record['session']))
        tx = directory / session
        entry(tx)
        plan_file = tx / 'plan.json'

        def save(phase, **fields):
            updated = dict(record, phase=phase, **fields)
            data = encode(updated)
            require(len(data) <= MAX_FILE, '原工作区切换日志超过上限')
            atomic_write(active, data)
            record.update(updated)

        def recover():
            if record['phase'] == 'applying':
                entry(plan_file)
                plan = json.loads(limited(plan_file))
                with native_publication_lock(home, plan['thread']):
                    undo(root, gitdir, tx, plan)
                save('rolled_back')
            if record['phase'] == 'reserved':
                save('rolled_back')

        def release():
            client.event(project, session, 'switch', 2, 'close')
            active.unlink()

        if prepared is None:
            recover()
            release()
            return record.get('result', dict(version=1, status='rolled_back', project=project))

        try:
            client.event(project, session, 'switch', 1, 'open')
            source_git, marker = prepared_receipt(prepared)
            lfs = inventory(marker)
            lfs_memo = {} if lfs else None
            before, ref = clean(root, lfs, lfs_memo)
            tx.mkdir()
            plan = prepare(root, gitdir, prepared, source_git, marker, tx, project, home, before, ref, automatic, lfs_memo)
            data = encode(plan)
            require(len(data) <= MAX_FILE, '原工作区切换日志超过上限')
            atomic_write(plan_file, data)
            with native_publication_lock(home, plan['thread']):
                if lfs:
                    require_cached(root, lfs)
                if automatic:
                    transcript = limited(tx / 'stage/.git/lanxi/session.jsonl')
                    require(session_progress(home, plan['thread'], transcript, distinguish_ahead=True)[0] != 'ahead',
                            '本机会话在自动切换期间取得了新进展')
                    revalidate()
                index_lock = gitdir / 'index.lock'
                require(not entry(index_lock), '原工作区索引正在使用')
                save('applying')
                os.link(tx / 'stage/.git/index', index_lock)
                if digest(gitdir / 'index') != digest(tx / 'old-index'):
                    index_lock.unlink()
                    save('rolled_back')
                    raise HandoffError('原工作区索引在切换期间发生变化')
                install_files(root, tx, plan)
                move_head(root, ref, before, plan['head'])
                index_lock.replace(gitdir / 'index')
                result = dict(version=1, status='applied', project=project, head=plan['head'], thread=plan['thread'],
                              cwd=str(root / plan['cwd']), backup=str(tx), files=len(plan['changes']) - len(RECEIPT_FILES))
                save('applied', result=result)
        except FAILURES:
            recover()
            release()
            raise
        release()
        return result
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('local-config.json'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--codex-home', type=Path, default=local_home())
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepared', type=Path)
    action.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    try:
        project = identifier(args.project)
        config = json.loads(limited(args.config, 2 * 1024 * 1024).decode('utf-8-sig'))
        client = device_client(config)
        require(client and client.token and project in client.paths.values(), '需要该项目的协调配置和设备令牌')
        prepared = None
        if args.prepared is not None:
            staging = absolute(config['handoffSync']['projects'][project]['receive']['staging'])
            root = next(path for path, name in client.paths.items() if name == project)
            folder = local_path(staging, project)
            prepared = args.prepared.resolve(strict=True)
            require(args.prepared.absolute() == prepared and prepared.parent == folder and separate(folder, root),
                    '请选择登记预备区下的独立合并目录')
        print(json.dumps(switch(client, project, prepared, args.codex_home.resolve())))
    except FAILURES as error:
        parser.exit(2, f'原工作区切换未完成：{getattr(error, "detail", None) or error}\n')


if __name__ == '__main__':
    main()
