"""Prefetch selected Git LFS objects into a repository's native LFS cache."""
import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from coordination_client import NoRedirect
from handoff import HandoffError, file_hash, git, local_path, names, require
from service_lock import ServiceLock


POINTER = re.compile(rb'version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize (0|[1-9][0-9]{0,18})\n')
RANGE = re.compile(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)')
MEDIA = 'application/vnd.git-lfs+json'
CHUNK = 1024 * 1024
BATCH_SIZE = 20
EMPTY_OID = hashlib.sha256().hexdigest()


class TransferError(ValueError):
    def __init__(self, status):
        self.status = status


def pointers(repo, ref, paths):
    paths = tuple(dict.fromkeys(paths))
    require(paths, '至少选择一个 Git LFS 文件')
    for name in paths:
        local_path(repo, name)
    commit = git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}', isolated=True).decode().strip()
    attributes = names(git(repo, 'check-attr', '--source=' + commit, '-z', '--stdin', 'filter',
                           data=''.join(name + '\0' for name in paths).encode(), isolated=True))
    require(attributes[2::3] == ['lfs'] * len(paths), '指定提交未对所选路径启用 Git LFS')
    tree = {}
    # Limit pathspecs per invocation to stay below the Windows command-line limit.
    for offset in range(0, len(paths), BATCH_SIZE):
        for entry in names(git(repo, 'ls-tree', '--full-tree', '-z', commit, '--', *paths[offset:offset + BATCH_SIZE],
                               isolated=True)):
            info, name = entry.split('\t', 1)
            mode, kind, blob = info.split()
            require(kind == 'blob' and mode in ('100644', '100755'), f'LFS 资源必须是普通文件：{name}')
            tree[name] = blob
    require(set(paths) == tree.keys(), '指定提交缺少要求的 LFS 路径')

    blobs = list(dict.fromkeys(tree.values()))
    request = ''.join(blob + '\n' for blob in blobs).encode()
    sizes = git(repo, 'cat-file', '--batch-check=%(objecttype) %(objectsize)', data=request, isolated=True).splitlines()
    require(len(sizes) == len(blobs) and all(row.startswith(b'blob ') and int(row.split()[1]) < 1024 for row in sizes),
            '指定提交包含非 LFS 指针文件')
    contents = git(repo, 'cat-file', '--batch', data=request, isolated=True)
    decoded = {}
    position = 0
    for blob, size in zip(blobs, sizes):
        position = contents.index(b'\n', position) + 1
        end = position + int(size.split()[1])
        pointer = contents[position:end]
        position = end + 1
        if not pointer:
            decoded[blob] = (EMPTY_OID, 0)
            continue
        match = POINTER.fullmatch(pointer)
        require(match, '需要标准 Git LFS v1 指针')
        oid, size = match.groups()
        decoded[blob] = (oid.decode(), int(size))
    return commit, {name: decoded[tree[name]] for name in paths}


def ordinary(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT, 'LFS 缓存路径包含链接或重解析点')
    return info


def cache_path(root, relative, create=False):
    path = root
    for part in relative.split('/'):
        path = path / part
        info = ordinary(path)
        if info is None and create:
            path.mkdir(exist_ok=True)
            info = ordinary(path)
        require(info is None or stat.S_ISDIR(info.st_mode), 'LFS 缓存路径已被文件占用')
    return path


def file_size(path):
    info = ordinary(path)
    require(info is None or stat.S_ISREG(info.st_mode), 'LFS 对象必须是普通文件')
    return info.st_size if info else None


def matches(path, oid, size):
    if file_size(path) != size:
        return False
    with path.open('rb') as stream:
        return file_hash(stream) == oid


class HTTPSource:
    def __init__(self, endpoint, username=None, token=None):
        self.endpoint = endpoint.rstrip('/')
        self.parts = urlsplit(self.endpoint)
        p = self.parts
        loopback = p.hostname in ('localhost', '127.0.0.1', '::1')
        require(p.hostname and p.username is None and not p.query and not p.fragment and p.path.endswith('/info/lfs')
                and (p.scheme == 'https' or (p.scheme == 'http' and loopback)), 'LFS 端点需要 HTTPS 或本机回环 HTTP')
        self.origin = self.identity(p)
        require(bool(username) == bool(token) and (username is None or ':' not in username), '需同时设置 LFS 用户和令牌')
        self.auth = ('Basic ' + base64.b64encode(f'{username}:{token}'.encode()).decode()) if token else None
        self.opener = build_opener(NoRedirect(), *([ProxyHandler({})] if loopback else []))

    @staticmethod
    def identity(parts):
        port = parts.port
        return parts.scheme, parts.hostname, port if port is not None else (443 if parts.scheme == 'https' else 80)

    def open(self, url, headers, data=None):
        try:
            return self.opener.open(Request(url, headers=headers, data=data), timeout=10)
        except HTTPError as error:
            error.close()
            statuses = {401: 'denied', 403: 'denied', 404: 'missing_remote', 410: 'missing_remote',
                        416: 'range_rejected'}
            raise TransferError(statuses.get(error.code, 'http_error')) from None
        except (URLError, OSError, http.client.HTTPException):
            raise TransferError('unavailable') from None

    def batch(self, objects):
        headers = {'Accept': MEDIA, 'Content-Type': MEDIA, 'Accept-Encoding': 'identity'}
        if self.auth:
            headers['Authorization'] = self.auth
        request = {'operation': 'download', 'transfers': ['basic'], 'hash_algo': 'sha256',
                   'objects': [{'oid': oid, 'size': size} for oid, size in objects.items()]}
        try:
            with self.open(self.endpoint + '/objects/batch', headers, json.dumps(request).encode()) as response:
                body = response.read(CHUNK + 1)
            document = json.loads(body) if len(body) <= CHUNK else {}
            rows = document['objects']
            result = {row['oid']: row for row in rows}
            if (document.get('transfer', 'basic') != 'basic' or document.get('hash_algo', 'sha256') != 'sha256'
                    or len(rows) != len(result) or result.keys() != objects.keys()
                    or any(type(row['size']) is not int or row['size'] != objects[oid] for oid, row in result.items())):
                raise TransferError('invalid_response')
            return result
        except (ValueError, KeyError, TypeError, OSError, http.client.HTTPException) as error:
            if isinstance(error, TransferError):
                raise
            raise TransferError('invalid_response') from None

    def download(self, row, offset):
        try:
            if 'error' in row:
                code = row['error']['code']
                raise TransferError('missing_remote' if code in (404, 410) else 'remote_error')
            action = row['actions']['download']
            url = action['href']
            parts = urlsplit(url)
            if (self.identity(parts) != self.origin or parts.username is not None or parts.fragment
                    or parts.path != self.parts.path + '/objects/' + row['oid']):
                raise TransferError('unsafe_download')
            auth = next((value for key, value in action.get('header', {}).items() if key.lower() == 'authorization'),
                        self.auth)
            headers = {'Accept': 'application/octet-stream', 'Accept-Encoding': 'identity'}
            if auth:
                headers['Authorization'] = auth
            if offset:
                headers['Range'] = f'bytes={offset}-'
            return self.open(url, headers)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            if isinstance(error, TransferError):
                raise
            raise TransferError('invalid_response') from None


def response_range(response, offset, size):
    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
        raise TransferError('invalid_response')
    if response.status == 200:
        start, end = 0, size
    elif response.status == 206 and offset:
        match = RANGE.fullmatch(response.headers.get('Content-Range', ''))
        if not match:
            raise TransferError('invalid_response')
        start, last, total = map(int, match.groups())
        if start != offset or total != size or not start <= last < size:
            raise TransferError('invalid_response')
        end = last + 1
    else:
        raise TransferError('invalid_response')
    length = response.headers.get('Content-Length')
    if length is not None and length != str(end - start):
        raise TransferError('invalid_response')
    return start, end


def publish(part, object_file, oid, size):
    try:
        os.link(part, object_file)
    except FileExistsError:
        if not matches(object_file, oid, size):
            raise TransferError('corrupt_cache') from None
    part.unlink()


def transfer(source, row, part, object_file):
    oid, size = row['oid'], row['size']
    offset = file_size(part) or 0
    if offset >= size and file_size(part) is not None:
        part.unlink()
        offset = 0
    if shutil.disk_usage(part.parent).free < size - offset:
        raise TransferError('insufficient_space')
    with source.download(row, offset) as response:
        start, end = response_range(response, offset, size)
        digest = hashlib.sha256()
        received = 0
        if start:
            with part.open('rb') as existing:
                for block in iter(lambda: existing.read(CHUNK), b''):
                    digest.update(block)
        with part.open('ab' if start else 'wb') as stream:
            try:
                while received < end - start:
                    block = response.read1(min(CHUNK, end - start - received))
                    if not block:
                        break
                    stream.write(block)
                    received += len(block)
                    digest.update(block)
                if received == end - start and response.read1(1):
                    # Drop untrusted bytes from this response and keep the previous prefix.
                    stream.truncate(start)
                    raise TransferError('invalid_response')
            except (OSError, http.client.HTTPException):
                return {'status': 'interrupted', 'receivedBytes': received, 'resumedFrom': start}
            if start + received < size:
                return {'status': 'interrupted', 'receivedBytes': received, 'resumedFrom': start}
            stream.flush()
            os.fsync(stream.fileno())
    if digest.hexdigest() != oid:
        part.unlink()
        return {'status': 'corrupt_download', 'receivedBytes': received, 'resumedFrom': start}
    publish(part, object_file, oid, size)
    return {'status': 'ready', 'receivedBytes': received, 'resumedFrom': start}


def prefetch(repo, ref, paths, source=None):
    repo = repo.resolve(strict=True)
    require(Path(git(repo, 'rev-parse', '--show-toplevel', isolated=True).decode().strip()).resolve() == repo,
            '请指定项目 Git 根目录')
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir',
                      isolated=True).decode().strip())
    require(common == repo / '.git' and ordinary(common) and common.is_dir(), '仅支持独立 Git 仓库的本机 LFS 缓存')
    require(not git(repo, 'config', '--get', 'lfs.storage', optional=True), '此命令使用默认 Git LFS 缓存路径')
    commit, selected = pointers(repo, ref, paths)
    objects = {}
    for oid, size in selected.values():
        require(objects.setdefault(oid, size) == size, 'LFS 指针的对象大小不一致')

    def run():
        reports, missing = {}, {}
        for oid, size in objects.items():
            folder = cache_path(common, f'lfs/objects/{oid[:2]}/{oid[2:4]}')
            location = folder / oid
            cached = file_size(location)
            if size == 0 and oid == EMPTY_OID:
                reports[oid] = {'status': 'ready'}
            elif cached is not None:
                reports[oid] = {'status': 'ready' if matches(location, oid, size) else 'corrupt_cache'}
            else:
                reports[oid] = {'status': 'missing_cache'}
                missing[oid] = size
        if source is None or not missing:
            return reports
        incomplete = cache_path(common, 'lfs/incomplete', create=True)
        for oid, size in list(missing.items()):
            part = incomplete / (oid + '.lanxi-part')
            if matches(part, oid, size):
                folder = cache_path(common, f'lfs/objects/{oid[:2]}/{oid[2:4]}', create=True)
                publish(part, folder / oid, oid, size)
                reports[oid] = {'status': 'ready', 'receivedBytes': 0, 'resumedFrom': size}
                del missing[oid]
        ids = list(missing)
        for offset in range(0, len(ids), BATCH_SIZE):
            batch = {oid: missing[oid] for oid in ids[offset:offset + BATCH_SIZE]}
            try:
                actions = source.batch(batch)
            except TransferError as error:
                reports.update({oid: {'status': error.status} for oid in batch})
                continue
            for oid, action in actions.items():
                folder = cache_path(common, f'lfs/objects/{oid[:2]}/{oid[2:4]}', create=True)
                try:
                    reports[oid] = transfer(source, action, incomplete / (oid + '.lanxi-part'), folder / oid)
                except TransferError as error:
                    reports[oid] = {'status': error.status}
        return reports

    if source is None:
        reports = run()
    else:
        storage = cache_path(common, 'lfs', create=True)
        lock_path = storage / 'lanxi-prefetch.lockfile'
        file_size(lock_path)
        try:
            lock = ServiceLock(lock_path)
        except RuntimeError:
            reports = {oid: {'status': 'busy'} for oid in objects}
        else:
            try:
                reports = run()
            finally:
                lock.close()
    files = {name: dict(oid=oid, size=size, **reports[oid]) for name, (oid, size) in selected.items()}
    return {'version': 1, 'commit': commit, 'status': 'ready' if all(row['status'] == 'ready' for row in files.values())
            else 'incomplete', 'files': files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--path', action='append', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--endpoint')
    action.add_argument('--check', action='store_true')
    parser.add_argument('--user-env')
    parser.add_argument('--token-env')
    args = parser.parse_args()
    try:
        username = os.environ.get(args.user_env) if args.user_env else None
        token = os.environ.get(args.token_env) if args.token_env else None
        require((args.user_env is None and args.token_env is None) or (args.endpoint and username and token),
                '需要已设置的 LFS 用户和令牌环境变量')
        source = HTTPSource(args.endpoint, username, token) if args.endpoint else None
        result = prefetch(args.repo, args.ref, args.path, source)
    except (HandoffError, OSError, ValueError, IndexError) as error:
        parser.exit(2, f'LFS 预取失败：{error}\n')
    print(json.dumps(result))
    return 0 if result['status'] == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
