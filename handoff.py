"""Seal and restore a completed Codex turn together with a Git worktree."""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class HandoffError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise HandoffError(message)


MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
DIGEST = re.compile('[0-9a-f]{64}')
GIT_ID = re.compile('[0-9a-f]{40}|[0-9a-f]{64}')
ROLLOUT = re.compile(r'rollout-(\d{4}-\d\d-\d\d)T\d\d-\d\d-\d\d-([0-9a-f-]{36})\.jsonl')
DIFF = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--ignore-submodules=none']
INDEX_DIFF = [*DIFF, '--cached', '--binary', '--full-index', '--no-color', '--src-prefix=a/', '--dst-prefix=b/']


def git_process(repo, *args, data=None, isolated=False, index_file=None):
    overrides = {'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_QUARANTINE_PATH',
                 'GIT_GLOB_PATHSPECS', 'GIT_NOGLOB_PATHSPECS', 'GIT_ICASE_PATHSPECS'}
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in overrides and not (isolated and k.upper().startswith('GIT_'))}
    if isolated:
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM='1',
                   GIT_NO_LAZY_FETCH='1', GIT_NO_REPLACE_OBJECTS='1')
    if index_file:
        env['GIT_INDEX_FILE'] = str(index_file)
    env.update(GIT_LITERAL_PATHSPECS='1', GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0',
               GIT_LFS_SKIP_SMUDGE='1', GIT_NO_LAZY_FETCH='1')
    return subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull,
                           '-c', 'core.fsmonitor=false', '-C', str(repo), *map(str, args)],
                          input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def git(repo, *args, data=None, optional=False, isolated=False, index_file=None):
    proc = git_process(repo, *args, data=data, isolated=isolated, index_file=index_file)
    if proc.returncode and not optional:
        raise HandoffError(f'Git {args[0]} 失败：{proc.stderr.decode("utf-8", "replace").strip()}')
    return None if proc.returncode else proc.stdout


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(file):
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def limited(path, limit=MAX_FILE):
    with path.open('rb') as stream:
        require(os.fstat(stream.fileno()).st_size <= limit, f'文件超过交接上限：{path.name}')
        data = stream.read(limit + 1)
    require(len(data) <= limit, '文件在封存期间增长')
    return data


def names(data):
    return [p.decode('utf-8') for p in data.split(b'\0') if p]


def local_path(root, name):
    require(isinstance(name, str) and name and '\\' not in name, '文件路径无效')
    path = root
    for part in name.split('/'):
        key = part.casefold()
        require(part not in ('', '.', '..') and part == part.rstrip(' .')
                and not re.search(r'[<>:"|?*\x00-\x1f]', part)
                and not re.fullmatch(r'(con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[1-9¹²³])',
                                     key.partition('.')[0].rstrip())
                and key != '.git' and not re.fullmatch(r'\.?git~\d+', key), '文件路径不适合跨机恢复')
        require(key not in ('.ssh', '.aws', '.codex', '.venv', 'node_modules', '__pycache__',
                            '.local', 'local-config.json', 'auth.json', '.git-credentials',
                            'id_rsa', 'id_ed25519') and not key.startswith('.env')
                and not key.endswith(('.pem', '.key', '.pfx', '.sqlite3')), f'私有或运行文件不能封存：{part}')
        path = path / part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT, '交接路径包含链接或重解析点')
    return path


def session_info(data):
    require(data.endswith(b'\n'), '会话记录尚未完整落盘')
    lines = data.splitlines()
    meta = json.loads(lines[0])
    require(meta['type'] == 'session_meta', '缺少原生会话元数据')
    thread = str(uuid.UUID(meta['payload']['id']))
    active = completed = None
    cwd = meta['payload']['cwd']
    for line in lines[1:]:
        event = json.loads(line)
        if event['type'] == 'turn_context':
            cwd = event['payload']['cwd']
        if event['type'] != 'event_msg':
            continue
        payload = event['payload']
        kind = payload['type']
        if kind == 'task_started':
            active = payload['turn_id']
            completed = None
        elif kind == 'task_complete':
            require(active and payload.get('turn_id') == active, '轮次开始与完成记录不匹配')
            completed, active = active, None
        elif kind in ('turn_aborted', 'user_message'):
            completed = None
    require(completed and not active, '最后一轮尚未确认完成，请等待 CLI 到达安全点')
    return thread, completed, cwd


def find_session(home, thread, include_compressed=False):
    require(str(uuid.UUID(thread)) == thread, '线程 ID 必须为标准 UUID')
    suffixes = ('.jsonl', '.jsonl.zst') if include_compressed else ('.jsonl',)
    return [p for folder in ('sessions', 'archived_sessions') for suffix in suffixes
            for p in (home / folder).rglob(f'rollout-*-{thread}{suffix}')]


def session_location(filename, thread):
    match = ROLLOUT.fullmatch(filename)
    require(match and match[2] == thread, '会话文件名与线程不匹配')
    day = date.fromisoformat(match[1])
    return Path('sessions', f'{day.year:04}', f'{day.month:02}', f'{day.day:02}', filename)


def check_object_sizes(repo, ids):
    if not ids:
        return
    result = git(repo, 'cat-file', '--batch-check=%(objecttype) %(objectsize)', data=ids)
    require(all(int(line.split()[1]) <= MAX_FILE for line in result.splitlines()), 'Git 变更包含超出上限的对象')


def checked_index(repo):
    head = git(repo, 'rev-parse', '--verify', 'HEAD').decode().strip()
    ref = git(repo, 'symbolic-ref', '-q', 'HEAD', optional=True)
    branch = ref.decode().strip().removeprefix('refs/heads/') if ref else None
    gitdir = Path(git(repo, 'rev-parse', '--absolute-git-dir').decode().strip())
    require(not any((gitdir / name).exists() for name in
                    ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer')),
            '请先结束正在进行的 Git 操作')
    require(not git(repo, 'ls-files', '--unmerged', '-z'), '请先处理索引中的合并冲突')
    entries = names(git(repo, 'ls-files', '-v', '--stage', '-z'))
    require(all(e.startswith('H ') and e[2:8] != '160000' for e in entries),
            '子模块、稀疏检出和特殊索引标志尚不支持')
    paths = ''.join(entry.split('\t', 1)[1] + '\0' for entry in entries).encode('utf-8')
    attrs = names(git(repo, 'check-attr', '-z', '--stdin', 'filter', data=paths))
    require(all(value in ('unset', 'unspecified') for value in attrs[2::3]),
            '项目依赖 Git 内容过滤器，请先完成独立对象交付')
    index = ''.join(entry[2:] + '\0' for entry in entries).encode('utf-8')
    raw = git(repo, *DIFF, '--cached', '--raw', '--no-abbrev', '-z')
    require(raw == git(repo, *DIFF, '--cached', '--raw', '--no-abbrev', '-z', '--ita-visible-in-index'),
            '暂不支持 intent-to-add 索引')
    changed = names(raw)
    ids = {oid for header in changed[::2] for oid in header.split()[2:4] if oid.strip('0')}
    check_object_sizes(repo, ''.join(oid + '\n' for oid in ids).encode())
    for item in changed[1::2]:
        local_path(repo, item)
    return head, branch, index


def git_state(repo, includes):
    head, branch, index = checked_index(repo)
    staged = git(repo, *INDEX_DIFF)
    dirty = names(git(repo, *DIFF, '--name-only', '-z'))
    extra = sorted(set(includes))
    for item in extra:
        local_path(repo, item)
        require(names(git(repo, 'ls-files', '--others', '--exclude-standard', '-z', '--', item)) == [item],
                f'新增文件须为未忽略的单个未跟踪文件：{item}')
    return head, branch, sha(index), staged, dirty, extra


def work_file(root, item):
    path = local_path(root, item)
    if not path.exists() or path.is_dir():
        return None, False
    require(path.is_file(), '仅支持普通工作区文件')
    return limited(path), bool(path.stat().st_mode & stat.S_IXUSR) if os.name != 'nt' else False


def capture(repo, home, project, base, thread, includes, output, previous_snapshot=None):
    repo, output = repo.resolve(), output.absolute()
    require(repo == Path(git(repo, 'rev-parse', '--show-toplevel').decode().strip()).resolve(),
            '请指定项目仓库根目录')
    require(not output.resolve().is_relative_to(repo) and not output.exists(), '请使用项目外尚不存在的输出文件')
    sessions = find_session(home, thread)
    require(len(sessions) == 1 and sessions[0].is_relative_to(home / 'sessions'), '需要唯一且未归档的指定会话')
    source = sessions[0]
    session_location(source.name, thread)
    transcript = limited(source)
    sid, turn, cwd = session_info(transcript)
    source_cwd = Path(cwd).resolve()
    require(sid == thread and source_cwd.is_dir() and source_cwd.is_relative_to(repo), '会话工作目录不属于指定项目')
    relative_cwd = source_cwd.relative_to(repo).as_posix()
    if relative_cwd != '.':
        local_path(repo, relative_cwd)
        require(Path(git(source_cwd, 'rev-parse', '--show-toplevel').decode().strip()).resolve() == repo,
                '会话目录属于另一个嵌套仓库')
    baseline = git(repo, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()
    state = git_state(repo, includes)
    head, branch, index_hash, staged, dirty, extra = state
    git(repo, 'merge-base', '--is-ancestor', baseline, head)
    manifest = dict(version=1, project=project, base=baseline, head=head, branch=branch,
                    index_hash=index_hash, cwd=relative_cwd, thread=thread, turn=turn,
                    rollout=source.name, files=[], objects={'index.patch': sha(staged), 'session.jsonl': sha(transcript)})
    for i, item in enumerate([*dirty, *extra]):
        data, executable = work_file(repo, item)
        key = None if data is None else f'files/{i}'
        if key:
            manifest['objects'][key] = sha(data)
        manifest['files'].append(dict(path=item, object=key, executable=executable, new=item in extra))
    snapshot = sha(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode())
    if snapshot == previous_snapshot:
        return None
    manifest['snapshot'] = snapshot
    if head != baseline:
        revision_range = baseline + '..' + head
        changed = names(git(repo, 'log', '-m', '--format=', '--name-only', '-z', '--no-renames', revision_range))
        for filename in set(changed):
            local_path(repo, filename)
        ids = git(repo, 'rev-list', '--objects', '--no-object-names', revision_range)
        check_object_sizes(repo, ids)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        archive = Path(tmp) / 'handoff.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            total = 0

            def put(key, data, limit=MAX_FILE):
                nonlocal total
                total += len(data)
                require(len(data) <= limit and total <= MAX_TOTAL, '交接包超过容量上限')
                bundle.writestr(key, data)

            if baseline != head:
                commits = Path(tmp) / 'commits.bundle'
                git(repo, 'bundle', 'create', commits, 'HEAD', '^' + baseline)
                data = limited(commits, MAX_TOTAL)
                put('commits.bundle', data, MAX_TOTAL)
                manifest['objects']['commits.bundle'] = sha(data)
            put('index.patch', staged)
            put('session.jsonl', transcript)
            for entry in manifest['files']:
                data, executable = work_file(repo, entry['path'])
                key = entry['object']
                require((sha(data) if data is not None else None) == manifest['objects'].get(key)
                        and executable == entry['executable'], '工作文件在封存期间发生变化，请重试')
                if key:
                    put(key, data)
            bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False).encode())
        require(state == git_state(repo, includes) and limited(source) == transcript,
                '项目或会话在封存期间发生变化，请重试')
        for entry in manifest['files']:
            data, executable = work_file(repo, entry['path'])
            require((sha(data) if data is not None else None) == manifest['objects'].get(entry['object'])
                    and executable == entry['executable'], '工作文件在封存期间发生变化，请重试')
        with archive.open('rb') as stream:
            checksum = file_hash(stream)
        os.link(archive, output)
    return dict(project=project, head=head, thread=thread, turn=turn, sha256=checksum, snapshot=snapshot)


def restore(package, checksum, project, seed, dest, expected=None):
    require(DIGEST.fullmatch(checksum), '请提供封存端返回的 SHA-256')
    seed, dest = seed.resolve(strict=True), dest.absolute()
    require(not dest.exists() and not dest.is_symlink() and not dest.resolve().is_relative_to(seed),
            '恢复目录必须是基线仓库以外的新目录')
    with package.open('rb') as stream, tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        require(os.fstat(stream.fileno()).st_size <= MAX_TOTAL + 2 * MAX_FILE, '交接包超过容量上限')
        require(file_hash(stream) == checksum, '交接包 SHA-256 不匹配')
        with zipfile.ZipFile(stream) as bundle:
            members = bundle.infolist()
            require(len(members) == len(set(bundle.namelist())) and sum(m.file_size for m in members)
                    <= MAX_TOTAL + MAX_FILE, '交接包目录无效或超过容量上限')
            require(bundle.getinfo('manifest.json').file_size <= MAX_FILE, '清单超过容量上限')
            m = json.loads(bundle.read('manifest.json'))
            require(m['version'] == 1 and m['project'] == project, '项目或交接格式版本不匹配')
            if expected:
                require(all(m.get(key) == value for key, value in expected.items()), '交接包与发布清单不符')
            base, head = m['base'], m['head']
            require(GIT_ID.fullmatch(base) and GIT_ID.fullmatch(head), 'Git 版本无效')
            require(set(bundle.namelist()) == {'manifest.json', *m['objects']}, '清单与对象列表不匹配')

            def get(key, limit=MAX_FILE):
                require(bundle.getinfo(key).file_size <= limit, '对象超过容量上限')
                data = bundle.read(key)
                require(sha(data) == m['objects'][key], '交接对象校验失败')
                return data

            transcript = get('session.jsonl')
            require(session_info(transcript)[:2] == (m['thread'], m['turn']), '轮次与清单不匹配')
            session_location(m['rollout'], m['thread'])
            work = Path(tmp) / 'work'
            work.mkdir()
            git(work, 'init', '--quiet', '--template=', '--object-format=' + ('sha256' if len(base) == 64 else 'sha1'))
            git(work, 'fetch', '--no-tags', '--', seed, base)
            if head != base:
                commits = Path(tmp) / 'commits.bundle'
                commits.write_bytes(get('commits.bundle', MAX_TOTAL))
                git(work, 'bundle', 'verify', commits)
                git(work, 'fetch', '--no-tags', '--', commits, 'HEAD')
                require(git(work, 'rev-parse', 'FETCH_HEAD').decode().strip() == head, 'Git 对象与清单不匹配')
            git(work, 'merge-base', '--is-ancestor', base, head)
            git(work, 'checkout', '--detach', head)
            if m['branch'] is not None:
                branch = m['branch']
                require(git(work, 'check-ref-format', '--branch', branch).decode().strip() == branch,
                        '无效的分支名')
                git(work, 'checkout', '-b', branch)
            origin = git(seed, 'remote', 'get-url', 'origin', optional=True)
            if origin:
                git(work, 'remote', 'add', '--', 'origin', origin.decode().strip())
            patch = get('index.patch')
            if patch:
                git(work, 'apply', '--binary', '--index', '--whitespace=nowarn', '-', data=patch)
            require(sha(git(work, 'ls-files', '--stage', '-z')) == m['index_hash'], '目标暂存区与源端不一致')
            dirty, extra = [], []
            # Remove tracked files first to allow a file to turn into a directory.
            entries = sorted(m['files'], key=lambda e: e['object'] is not None)
            for e in entries:
                path = local_path(work, e['path'])
                if e['new']:
                    if path.is_dir():
                        path.rmdir()
                    require(not path.exists() and e['object'] is not None, '新增路径已被占用')
                    extra.append(e['path'])
                else:
                    require(names(git(work, 'ls-files', '-z', '--', e['path'])) == [e['path']],
                            '工作区文件与索引不匹配')
                    dirty.append(e['path'])
                if e['object'] is None:
                    path.unlink()
                    parent = path.parent
                    while parent != work and not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                else:
                    data = get(e['object'])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    if os.name != 'nt':
                        path.chmod(0o755 if e['executable'] else 0o644)
            actual = names(git(work, *DIFF, '--name-only', '-z'))
            require(sorted(dirty) == sorted(actual) and sorted(extra) == sorted(names(
                    git(work, 'ls-files', '--others', '--exclude-standard', '-z'))), '目标工作区状态与清单不匹配')
            private = work / '.git' / 'lanxi'
            mapped = work if m['cwd'] == '.' else local_path(work, m['cwd'])
            require(mapped.is_dir(), '目标快照缺少会话工作目录')
            private.mkdir()
            (private / 'session.jsonl').write_bytes(transcript)
            (private / 'handoff.json').write_text(json.dumps(dict(
                version=1, project=project, head=head, package_sha256=checksum, cwd=m['cwd'],
                thread=m['thread'], turn=m['turn'], rollout=m['rollout'],
                sha256=m['objects']['session.jsonl'], included=sorted(extra))), encoding='utf-8')
        require(not dest.exists() and not dest.is_symlink(), '目标目录已被占用')
        work.rename(dest)
    return dict(project=project, cwd=str(dest / m['cwd']), head=head, thread=m['thread'], turn=m['turn'])


def handoff_receipts(projects):
    """Read restore receipts only from the workspaces registered on this device."""
    results = []
    for project, location in projects.items():
        row = dict(project=project, status='awaiting_restore')
        try:
            root = Path(location)
            require(root.is_absolute(), '项目映射需要绝对路径')
            marker = root / '.git' / 'lanxi' / 'handoff.json'
            try:
                record = json.loads(limited(marker, 65536))
            except FileNotFoundError:
                pass
            else:
                require(record['version'] == 1 and record['project'] == project, '交接项目不匹配')
                require(GIT_ID.fullmatch(record['head']) and DIGEST.fullmatch(record['package_sha256'])
                        and isinstance(record['thread'], str) and str(uuid.UUID(record['thread'])) == record['thread']
                        and isinstance(record['turn'], str) and record['turn'],
                        '交接记录不完整')
                row.update(status='restored', head=record['head'], thread=record['thread'],
                           turn=record['turn'], sha256=record['package_sha256'])
        except (KeyError, OSError, TypeError, ValueError):
            row = dict(project=project, status='invalid_receipt')
        results.append(row)
    return results


def session_progress(home, thread, transcript):
    matches = find_session(home, thread, include_compressed=True)
    if not matches:
        return 'missing', None, None
    require(len(matches) == 1 and matches[0].is_relative_to(home / 'sessions') and matches[0].suffix == '.jsonl',
            '目标机存在同 ID 的归档、压缩或多份记录，请先核对')
    path = local_path(home, matches[0].relative_to(home).as_posix())
    with path.open('rb') as stream:
        prefix = stream.read(len(transcript) + 1)
    if prefix.startswith(transcript):
        return 'current', path, None
    if prefix and transcript.startswith(prefix):
        try:
            sid, _, _ = session_info(prefix)
        except (ValueError, KeyError, TypeError, IndexError):
            pass
        else:
            if sid == thread:
                return 'advance', path, prefix
    raise HandoffError('目标机同一线程已有不同或未完成的历史，已保留双方内容')


@contextmanager
def native_publication_lock(home, thread):
    import msvcrt

    def lock(file, label):
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise HandoffError(label) from error

    # Codex 0.169.1 takes the home coordination lock before opening the
    # thread writer lock. Keep both handles alive until publication finishes.
    directory = local_path(home, 'thread-writer-locks')
    directory.mkdir(exist_ok=True)
    with local_path(directory, '.coordination.lock').open('a+b') as coordination:
        lock(coordination, 'Codex 正在协调历史写入，请稍后续接')
        with local_path(directory, thread + '.lock').open('a+b') as writer:
            lock(writer, '同一线程仍有 Codex 原生写入者，请退出旧会话后续接')
            yield


@contextmanager
def staged_rollout(directory, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.lanxi-rollout-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def advance_session(home, thread, path, prefix, transcript, executable):
    require(executable and os.name == 'nt', '历史快进需要通过已验证的 Windows Codex CLI 启动')
    version = subprocess.run([str(executable), '--version'], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=5)
    require(version.returncode == 0 and version.stdout.strip() == 'codex-cli 0.169.1',
            '当前 Codex CLI 版本的原生写入锁尚未验证，已保留两端历史')
    with staged_rollout(path.parent, transcript) as new, staged_rollout(path.parent, prefix) as previous:
        with native_publication_lock(home, thread):
            state, current, data = session_progress(home, thread, transcript)
            if state == 'current':
                return
            require(state == 'advance' and path == current and data == prefix, '本机历史刚刚变化，请重新续接')
            backups = local_path(home, 'lanxi-handoff-backups/' + thread)
            backups.mkdir(parents=True, exist_ok=True)
            backup = local_path(backups, sha(prefix) + '.jsonl')
            try:
                os.link(previous, backup)
            except FileExistsError:
                require(limited(backup) == prefix, '本机旧历史备份校验失败')
            new.replace(path)


@dataclass
class ResumeContext:
    project: str
    thread: str
    cwd: Path
    private: Path
    rollout: str
    turn: str
    checksum: str


def resume_context(cwd):
    root = git(cwd, 'rev-parse', '--show-toplevel', optional=True)
    if not root:
        return None
    root = Path(root.decode().strip())
    private = Path(git(root, 'rev-parse', '--absolute-git-dir').decode().strip()) / 'lanxi'
    marker = private / 'handoff.json'
    if not marker.exists():
        return None
    m = json.loads(limited(marker))
    require(m['version'] == 1, '续接记录版本不支持')
    mapped = root if m['cwd'] == '.' else local_path(root, m['cwd'])
    require(mapped.is_dir(), '目标会话工作目录已不存在')
    return ResumeContext(m['project'], m['thread'], mapped, private, m['rollout'], m['turn'], m['sha256'])


def install_resume(context, home, executable=None):
    data = limited(context.private / 'session.jsonl')
    require(sha(data) == context.checksum and session_info(data)[:2] == (context.thread, context.turn),
            '本地交接记录损坏')
    target = local_path(home, session_location(context.rollout, context.thread).as_posix())
    state, path, prefix = session_progress(home, context.thread, data)
    if state == 'advance':
        advance_session(home, context.thread, path, prefix, data, executable)
    elif state == 'missing':
        target.parent.mkdir(parents=True, exist_ok=True)
        with staged_rollout(target.parent, data) as tmp:
            try:
                os.link(tmp, target)
            except FileExistsError:
                pass
        require(session_progress(home, context.thread, data)[0] == 'current', '安装后的会话未找到')
    return dict(thread=context.thread, cwd=str(context.cwd))


def prepare_resume(cwd, home, executable=None):
    context = resume_context(cwd)
    return install_resume(context, home, executable) if context else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    pack = commands.add_parser('capture', help='封存已完成轮次和对应的 Git 状态')
    unpack = commands.add_parser('restore', help='在新的项目目录还原已校验的快照')
    launch = commands.add_parser('prepare-resume', help='为当前项目准备原生 CLI 续接')
    for command in (pack, launch):
        command.add_argument('--codex-home', type=Path,
                             default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    for command in (pack, unpack):
        command.add_argument('--project', required=True, help='各设备一致的稳定项目标识')
    pack.add_argument('--repo', type=Path, required=True)
    pack.add_argument('--base', required=True, help='两端已有的共同提交或引用')
    pack.add_argument('--thread', required=True)
    pack.add_argument('--include', action='append', default=[], help='准许传输的未跟踪文件，相对项目路径')
    pack.add_argument('--out', type=Path, required=True)
    unpack.add_argument('--package', type=Path, required=True)
    unpack.add_argument('--sha256', required=True)
    unpack.add_argument('--seed', type=Path, required=True, help='目标机拥有共同提交的本地仓库')
    unpack.add_argument('--dest', type=Path, required=True)
    launch.add_argument('--cwd', type=Path, default=Path.cwd())
    launch.add_argument('--codex', type=Path, help='验证原生写入锁后允许快进的 Codex CLI')
    args = parser.parse_args()
    try:
        if args.action == 'capture':
            result = capture(args.repo, args.codex_home.resolve(), args.project, args.base,
                             args.thread, args.include, args.out)
        elif args.action == 'restore':
            result = restore(args.package, args.sha256, args.project, args.seed, args.dest)
        else:
            result = prepare_resume(args.cwd, args.codex_home.resolve(), args.codex)
        print(json.dumps(result))
    except (ValueError, OSError, KeyError, IndexError, TypeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f'交接失败：{exc}\n')


if __name__ == '__main__':
    main()
