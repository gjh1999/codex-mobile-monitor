"""Store and verify bounded, resumable compute output transfers."""
import json
import os
from contextlib import contextmanager

from coordination import Conflict
from handoff import DIGEST, limited, local_path, require, sha
from handoff_sync import atomic_write
from lfs_assets import file_size, matches
from service_lock import ServiceLock


CHUNK = 4 * 1024**2
MANIFEST_LIMIT = 2 * 1024**2
OUTPUT_LIMIT = 128 * 1024**2
OUTPUT_COUNT = 128


def manifest(raw, root, project, job):
    try:
        data = json.loads(raw)
        expected = dict(version=1, project=project, id=job['id'], attempt=job['attempt'],
                        commit=job['commit'], name=job['profile'], exitCode=0, outcome='exited')
        require(all(type(data[key]) is type(value) and data[key] == value for key, value in expected.items()),
                '产物清单与作业不一致')
        outputs = data['outputs']
        require(isinstance(outputs, list) and 0 < len(outputs) <= OUTPUT_COUNT, '产物清单数量无效')
        total, leaves, parents = 0, set(), {}
        for item in outputs:
            name, size, digest = item['path'], item['bytes'], item['sha256']
            local_path(root, name)
            key = name.casefold()
            require(key not in leaves and key not in parents, '产物路径冲突')
            leaves.add(key)
            parent = name.rpartition('/')[0]
            while parent:
                key = parent.casefold()
                require(key not in leaves and parents.setdefault(key, parent) == parent, '产物目录冲突')
                parent = parent.rpartition('/')[0]
            require(type(size) is int and 0 <= size <= OUTPUT_LIMIT - total
                    and isinstance(digest, str) and DIGEST.fullmatch(digest)
                    and (size or digest == sha(b'')), '产物大小或散列无效')
            total += size
        return outputs
    except (TypeError, KeyError, IndexError) as error:
        raise ValueError('产物清单格式无效') from error


def append_block(part, data):
    with part.open('ab') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class ArtifactStore:
    def __init__(self, root):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def directory(self, project, job):
        return local_path(self.root, f"p-{sha(project.encode())}/{job['id']}/{job['attempt']}")

    @contextmanager
    def locked(self, directory):
        try:
            lock = ServiceLock(local_path(directory, 'lockfile'))
        except RuntimeError as error:
            raise Conflict('当前作业正在上传另一块产物') from error
        try:
            yield
        finally:
            lock.close()

    def load(self, directory, project, job):
        raw = limited(local_path(directory, 'manifest.json'), MANIFEST_LIMIT)
        if job['status'] == 'finished' and sha(raw) != job['resultSha256']:
            raise FileNotFoundError('没有与完成回执匹配的产物')
        return raw, manifest(raw, directory / 'names', project, job)

    @staticmethod
    def paths(directory, index):
        return local_path(directory, f'{index}.part'), local_path(directory, f'{index}.output')

    def progress(self, directory, index, item):
        part, final = self.paths(directory, index)
        expected = item['bytes']
        stored = file_size(final)
        if stored is not None:
            require(stored == expected, '产物已落盘文件的大小不符')
            return stored
        observed = file_size(part)
        if observed is None and expected:
            return 0
        if observed is None:
            part.touch(exist_ok=False)
        size = observed or 0
        if size >= expected:
            if not matches(part, item['sha256'], expected):
                part.unlink()
                raise ValueError('上传产物的长度或 SHA-256 校验失败，分块可重新上传')
            part.replace(final)
        return size

    def stage(self, project, job, raw):
        directory = self.directory(project, job)
        digest = sha(raw)
        if job['status'] == 'finished':
            require(digest == job['resultSha256'], '产物清单与最终回执不匹配')
        outputs = manifest(raw, directory / 'names', project, job)
        directory.mkdir(parents=True, exist_ok=True)
        with self.locked(directory):
            path = local_path(directory, 'manifest.json')
            if path.exists():
                if limited(path, MANIFEST_LIMIT) != raw:
                    raise Conflict('此作业尝试已有其他产物清单')
            else:
                atomic_write(path, raw)
            return {'sha256': digest, 'offsets': [self.progress(directory, index, item)
                                                  for index, item in enumerate(outputs)]}

    def upload(self, project, job, index, offset, data):
        directory = self.directory(project, job)
        _, outputs = self.load(directory, project, job)
        if not 0 <= index < len(outputs):
            raise FileNotFoundError('产物编号不存在')
        item = outputs[index]
        size = item['bytes']
        require(0 <= offset < size and len(data) == min(CHUNK, size - offset), '产物分块长度或偏移无效')
        with self.locked(directory):
            current = self.progress(directory, index, item)
            if offset < current:
                return {'offset': current}
            if offset != current:
                raise Conflict('上传偏移超前，请重新读取产物进度')
            part, _ = self.paths(directory, index)
            append_block(part, data)
            return {'offset': self.progress(directory, index, item)}

    def available(self, directory, index, item):
        _, final = self.paths(directory, index)
        if file_size(final) != item['bytes']:
            raise FileNotFoundError('产物尚未完整上传')
        return final

    def download_manifest(self, project, job):
        directory = self.directory(project, job)
        raw, outputs = self.load(directory, project, job)
        for index, item in enumerate(outputs):
            self.available(directory, index, item)
        return raw

    def download(self, project, job, index, offset):
        directory = self.directory(project, job)
        _, outputs = self.load(directory, project, job)
        if not 0 <= index < len(outputs):
            raise FileNotFoundError('产物编号不存在')
        item = outputs[index]
        require(0 <= offset < item['bytes'], '产物读取偏移无效')
        length = min(CHUNK, item['bytes'] - offset)
        with self.available(directory, index, item).open('rb') as stream:
            stream.seek(offset)
            data = stream.read(length)
        require(len(data) == length, '产物在读取时发生变化')
        return data
